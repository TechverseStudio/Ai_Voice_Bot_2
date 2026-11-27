"""Durable Queue Worker for Autonomous Campaign Calling.

Provides safe multi-worker execution, atomic job claiming, carrier-aware channel
concurrency reservation, pre-dispatch DND/calling-window verification, automatic
stale-lease recovery, and end-to-end simulation or live PSTN execution.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
import signal
import sys
from typing import Any
from uuid import uuid4

from backend.analysis import analyze_conversation
from backend.agent.conversation_manager import ConversationManager
from backend.config import (
    LIVE_CALLS_ENABLED,
    SIMULATION_MODE,
    TELEPHONY_PROVIDER,
)
from backend.crm import (
    Campaign,
    CampaignJob,
    SessionLocal,
    claim_next_campaign_job,
    create_follow_up_task,
    create_or_update_lead,
    get_campaign,
    get_campaign_contacts,
    get_dnd_entries,
    is_phone_dnd,
    log_audit,
    reconcile_expired_channels,
    recover_stale_jobs,
    release_channel_capacity,
    reserve_channel_capacity,
    save_contacts,
    save_outbound_call,
    update_campaign_contact_status,
    update_campaign_status,
    update_job_status,
)
from backend.scheduler import validate_and_book_appointment
from backend.outbound import CallRequest, initiate_outbound_call
from backend.telephony import get_configured_provider_name, get_provider

logger = logging.getLogger("campaign_worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s")


class CampaignWorker:
    """Autonomous queue worker instance."""

    def __init__(self, worker_id: str | None = None, poll_interval: float = 1.0) -> None:
        self.worker_id = worker_id or f"worker-{os.getpid()}-{uuid4().hex[:6]}"
        self.poll_interval = poll_interval
        self.is_running = False
        self.conversation_manager = ConversationManager()
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Start the worker processing loop."""
        self.is_running = True
        logger.info(f"Campaign Worker '{self.worker_id}' started. Simulation mode: {SIMULATION_MODE}")

        # 1. Recover stale leases from any previously crashed workers
        recovered = recover_stale_jobs(self.worker_id)
        if recovered:
            logger.info(f"[{self.worker_id}] Recovered {recovered} stale job(s) from crashed workers.")

        # 2. Main execution loop
        while self.is_running and not self._shutdown_event.is_set():
            try:
                # Reconcile expired channel safety locks
                reconcile_expired_channels()

                # Process one eligible job
                job_processed = await self.process_next_job()
                if not job_processed:
                    # Sleep when no work available
                    await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[{self.worker_id}] Worker loop error: {e}")
                await asyncio.sleep(self.poll_interval)

        logger.info(f"Campaign Worker '{self.worker_id}' stopped.")

    def stop(self) -> None:
        """Signal worker to stop gracefully."""
        self.is_running = False
        self._shutdown_event.set()

    async def process_next_job(self, campaign_id: int | None = None) -> bool:
        """Attempt to claim and process a single job atomically.

        Returns True if a job was claimed and handled, False otherwise.
        """
        # 1. Atomically claim next job
        job = claim_next_campaign_job(
            worker_id=self.worker_id,
            campaign_id=campaign_id,
            lease_seconds=120,
        )
        if not job:
            return False

        logger.info(f"[{self.worker_id}] Claimed Job #{job.id} for Contact #{job.contact_id} ({job.phone_number})")

        campaign = get_campaign(job.campaign_id)
        if not campaign or campaign.status != "running":
            # Release job if campaign is paused or not running
            update_job_status(job.id, status="pending", last_error="Campaign paused or stopped")
            return True

        # 2. Pre-Dispatch Check: DND & Opt-Out
        if is_phone_dnd(job.phone_number):
            logger.info(f"[{self.worker_id}] Job #{job.id} blocked by DND Registry: {job.phone_number}")
            update_job_status(job.id, status="dnd_blocked", outcome="dnd_opt_out")
            log_audit("DND_BLOCKED", "CampaignJob", str(job.id), f"Phone {job.phone_number} on DND")
            return True

        # 3. Pre-Dispatch Check: Calling Window
        in_window, window_msg = self.is_within_calling_window(campaign)
        if not in_window:
            logger.info(f"[{self.worker_id}] Job #{job.id} outside calling window: {window_msg}")
            # Defer job by 60 minutes or window start
            next_time = datetime.now(timezone.utc) + timedelta(minutes=60)
            update_job_status(
                job.id,
                status="retry_scheduled",
                outcome="outside_window",
                next_scheduled_for=next_time,
                last_error=window_msg,
            )
            return True

        # 4. Pre-Dispatch Check: Max Attempts Reached
        if job.attempt >= job.max_attempts:
            logger.info(f"[{self.worker_id}] Job #{job.id} reached max attempts ({job.max_attempts})")
            update_job_status(job.id, status="max_retries_reached", outcome="unreachable")
            return True

        # 5. Reserve Channel Capacity before dispatch
        channel = reserve_channel_capacity(
            campaign_id=campaign.id,
            job_id=job.id,
            phone_number=job.phone_number,
            timeout_seconds=600,
        )
        if not channel:
            # Capacity full! Release claim back to pending so other jobs or retries wait
            logger.info(f"[{self.worker_id}] Campaign #{campaign.id} at capacity limit ({campaign.concurrency_limit}). Releasing job #{job.id} to wait.")
            update_job_status(job.id, status="pending", last_error="Channel capacity full")
            return True

        # 6. Persist call attempt count BEFORE dispatch
        with SessionLocal() as session:
            j = session.get(CampaignJob, job.id)
            if j:
                j.attempt += 1
                j.status = "dispatching"
                session.commit()
                job.attempt = j.attempt

        # 7. Execute Call Dispatch (Simulation vs Live PSTN)
        try:
            if not LIVE_CALLS_ENABLED or SIMULATION_MODE or TELEPHONY_PROVIDER == "mock":
                await self._execute_simulated_dispatch(campaign, job)
            else:
                await self._execute_live_dispatch(campaign, job)
        except Exception as e:
            logger.exception(f"[{self.worker_id}] Dispatch failure for Job #{job.id}: {e}")
            release_channel_capacity(job_id=job.id)
            update_job_status(job.id, status="failed", last_error=str(e))

        return True

    def is_within_calling_window(self, campaign: Campaign) -> tuple[bool, str]:
        """Verify local time satisfies permitted calling window (e.g. 09:30 to 19:30)."""
        is_simulation = (
            os.getenv("SIMULATION_MODE", "true").lower() in ("true", "1", "yes")
            or not LIVE_CALLS_ENABLED
            or TELEPHONY_PROVIDER == "mock"
        )
        if is_simulation:
            return True, "Simulation mode bypasses calling window"

        now = datetime.now()
        current_time_str = now.strftime("%H:%M")
        window_start = campaign.window_start or "09:30"
        window_end = campaign.window_end or "19:30"

        if window_start <= current_time_str <= window_end:
            return True, f"Within calling window ({window_start} - {window_end})"
        return False, f"Outside calling window ({window_start} - {window_end})"

    async def _execute_simulated_dispatch(self, campaign: Campaign, job: CampaignJob) -> None:
        """Run safe end-to-end simulated call progression."""
        session_id = f"sim-{uuid4().hex[:12]}"
        phone = job.phone_number
        name = job.customer_name or "Prospective Buyer"

        update_job_status(job.id, status="active", session_id=session_id, provider_call_id=session_id)

        # Track call record in database
        call_rec = save_outbound_call(
            lead_id=None,
            phone_number=phone,
            provider_call_id=session_id,
            status="simulated_accepted",
            message=f"Simulated call dispatched to {name} ({phone[-4:]})",
            provider="mock",
            campaign_id=campaign.id,
        )

        # Determine scenario based on notes or phone flags
        scenario = self._determine_simulation_scenario(name, job.locality, phone)
        logger.info(f"[{self.worker_id}] Executing scenario '{scenario}' for {name} ({phone})")

        if scenario == "busy":
            await asyncio.sleep(0.05)
            self._handle_simulated_terminal(job, call_rec.provider_call_id, "busy", "Line Busy")
            return

        if scenario == "no_answer":
            await asyncio.sleep(0.05)
            self._handle_simulated_terminal(job, call_rec.provider_call_id, "no_answer", "No Answer")
            return

        if scenario == "provider_failure":
            await asyncio.sleep(0.05)
            self._handle_simulated_terminal(job, call_rec.provider_call_id, "failed", "Carrier network timeout")
            return

        # Connected dialogue simulation
        session = self.conversation_manager.get_or_create_session(
            session_id=session_id,
            customer_name=name,
            agent_name=campaign.agent_name.split("-")[0].strip(),
            language=campaign.language or "en-IN",
        )
        greeting = self.conversation_manager.generate_opening_greeting(session)
        transcript_lines = [f"Agent: {greeting}"]

        dialogue_turns = self._generate_dialogue_for_scenario(scenario, name, job.locality)
        for user_speech in dialogue_turns:
            transcript_lines.append(f"Customer: {user_speech}")
            agent_reply, intent, facts = self.conversation_manager.process_turn(session_id, user_speech)
            transcript_lines.append(f"Agent: {agent_reply}")
            if session.is_concluded:
                break
            await asyncio.sleep(0.01)

        full_transcript = "\n".join(transcript_lines)
        analysis = analyze_conversation(full_transcript, customer_name_fallback=name, locality_fallback=job.locality)

        # Handle Opt-Out (DND)
        if session.opt_out or analysis.get("category") == "Do Not Call":
            from backend.crm import add_dnd_entry
            add_dnd_entry(phone, reason="Customer requested DND during AI call")
            self._handle_simulated_terminal(job, session_id, "dnd_blocked", "Do Not Call", analysis, full_transcript)
            return

        # Handle Lead & CRM Update
        reqs = analysis.get("requirements", {})
        lead = create_or_update_lead(
            name=name if name and name != "there" else f"Caller ({phone[-4:]})",
            phone_number=phone,
            area=reqs.get("preferred_locality") or job.locality or "Wakad",
            bhk=str(reqs.get("bhk_formatted") or reqs.get("bhk") or ""),
            budget=str(reqs.get("budget") or ""),
            purpose=str(reqs.get("purpose") or ""),
            timeline=str(reqs.get("timeline") or ""),
            financing=str(reqs.get("financing_requirement") or ""),
            amenities=str(reqs.get("amenities_preferred") or ""),
            site_visit_interest=bool(reqs.get("site_visit_interest")),
            interest_status=analysis.get("category", "Qualified"),
            score=analysis.get("score") or 0,
            summary=analysis.get("summary", ""),
            extracted_requirements=reqs,
            evidence=analysis.get("evidence", {}),
            next_action=analysis.get("next_action", ""),
            campaign_id=campaign.id,
            assigned_salesperson=campaign.assigned_sales_team or "Wakad Sales Desk",
        )

        # Handle Appointment if requested
        if analysis.get("requirements", {}).get("site_visit_interest"):
            reqs = analysis["requirements"]
            validate_and_book_appointment(
                lead_id=lead.id,
                requested_date_raw=reqs.get("preferred_meeting_date") or "Saturday",
                requested_time_raw=reqs.get("preferred_meeting_time") or "11:00 AM",
                project_name=campaign.project_name,
                customer_notes=f"Simulated booking from campaign #{campaign.id}",
                campaign_id=campaign.id,
            )

        # Handle Human Escalation Task
        if analysis.get("human_escalation_requested") or session.facts_collected.get("human_escalation_requested"):
            create_follow_up_task(
                lead_id=lead.id,
                campaign_id=campaign.id,
                customer_name=name,
                phone_number=phone,
                task_type="human_escalation",
                priority="high",
                notes=f"Caller requested human agent during simulated campaign '{campaign.name}'.",
                assigned_to=campaign.assigned_sales_team or "Senior Property Specialist",
            )
            log_audit("HUMAN_ESCALATION_TASK", "Lead", str(lead.id), "Customer requested human sales specialist")

        self._handle_simulated_terminal(job, session_id, "completed", analysis.get("category", "Qualified"), analysis, full_transcript, lead_id=lead.id)

    def _handle_simulated_terminal(
        self,
        job: CampaignJob,
        call_id: str,
        status: str,
        outcome: str,
        analysis: dict | None = None,
        transcript: str = "",
        lead_id: int | None = None,
    ) -> None:
        """Finalize call outcome, manage retry scheduling, and release channel capacity."""
        from backend.crm import update_outbound_call_status, save_call

        duration = 60 if status == "completed" else 0
        update_outbound_call_status(
            provider_call_id=call_id,
            status="completed" if status == "completed" else status,
            duration_seconds=duration,
        )

        if transcript and lead_id:
            try:
                save_call(
                    lead_id=lead_id,
                    transcript=transcript,
                    agent_response=outcome,
                    interest_status=analysis.get("category", outcome) if analysis else outcome,
                    campaign_id=job.campaign_id,
                    session_id=call_id,
                    call_duration_seconds=duration,
                )
            except Exception as e:
                logger.warning(f"Failed to save call item: {e}")

        # Check retry condition for busy or no_answer
        if status in ("busy", "no_answer", "failed") and job.attempt < job.max_attempts:
            # Schedule retry in 30 seconds for simulation (or 60 mins production)
            retry_time = datetime.now(timezone.utc) + timedelta(seconds=30)
            update_job_status(
                job.id,
                status="retry_scheduled",
                outcome=outcome,
                next_scheduled_for=retry_time,
                last_error=f"Call ended with {status}",
            )
            logger.info(f"[{self.worker_id}] Job #{job.id} ({job.phone_number}) scheduled for retry at {retry_time.isoformat()}")
        else:
            final_status = "completed" if status == "completed" else ("dnd_blocked" if status == "dnd_blocked" else "failed")
            update_job_status(job.id, status=final_status, outcome=outcome)

        release_channel_capacity(job_id=job.id)

    async def _execute_live_dispatch(self, campaign: Campaign, job: CampaignJob) -> None:
        """Place real outbound PSTN phone call through Exotel."""
        phone = job.phone_number
        name = job.customer_name or "Prospective Buyer"

        provider = get_provider(TELEPHONY_PROVIDER)
        ready, msg, missing = provider.is_ready()
        if not ready:
            raise RuntimeError(f"Telephony provider '{TELEPHONY_PROVIDER}' is not ready: {msg}")

        call_req = CallRequest(
            phone_number=phone,
            campaign_id=campaign.id,
            customer_name=name,
            context={"agent_name": campaign.agent_name, "project_name": campaign.project_name},
        )
        result = initiate_outbound_call(call_req)
        save_outbound_call(
            lead_id=None,
            phone_number=phone,
            provider_call_id=result.provider_call_id,
            status=result.status,
            message=result.message,
            provider=result.provider,
            campaign_id=campaign.id,
        )

        update_job_status(
            job.id,
            status="active",
            provider_call_id=result.provider_call_id,
            session_id=result.provider_call_id,
        )
        logger.info(f"[{self.worker_id}] Real PSTN Call initiated: {result.provider_call_id} to {phone}")

    def _determine_simulation_scenario(self, name: str, locality: str, phone: str) -> str:
        """Resolve simulation scenario from test triggers or customer metadata."""
        n_lower = name.lower()
        if "busy" in n_lower or phone.endswith("0001"):
            return "busy"
        if "no answer" in n_lower or phone.endswith("0002"):
            return "no_answer"
        if "opt out" in n_lower or "dnd" in n_lower or phone.endswith("0003"):
            return "dnd_opt_out"
        if "human" in n_lower or "manager" in n_lower or phone.endswith("0004"):
            return "human_escalation"
        if "fail" in n_lower or phone.endswith("0005"):
            return "provider_failure"
        if "warm" in n_lower or phone.endswith("0006"):
            return "answered_warm"
        return "answered_hot"

    def _generate_dialogue_for_scenario(self, scenario: str, name: str, locality: str) -> list[str]:
        if scenario == "human_escalation":
            return [
                "Yes I have a minute.",
                "Actually I don't want to talk to an AI bot, please connect me to a human sales manager.",
            ]
        if scenario == "dnd_opt_out":
            return [
                "Stop calling this number. Please put me on your do not call list immediately.",
            ]
        if scenario == "answered_warm":
            return [
                "Yes, I have two minutes. I might be looking for a 1 BHK flat later this year.",
                "What is the starting price for 1 BHK?",
                "Okay, please send details on WhatsApp for now. I am exploring.",
            ]
        # Default: answered_hot
        return [
            "Yes, I have two minutes. I am looking to buy a 2 BHK apartment near Wakad.",
            "My budget is around 75 lakhs. Can I visit this Saturday at 11 AM?",
            "Yes Saturday 11 AM works perfectly for me. Please confirm the visit.",
        ]


async def run_worker() -> None:
    worker = CampaignWorker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            pass  # Windows signal handling
    await worker.start()


if __name__ == "__main__":
    asyncio.run(run_worker())
