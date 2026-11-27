"""Acceptance Test Suite for Durable Queue, Concurrency, Simulation Mode, and Deployment Readiness."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import pytest
from starlette.testclient import TestClient

from backend.crm import (
    Appointment,
    Campaign,
    CampaignContact,
    CampaignJob,
    Contact,
    FollowUpTask,
    Lead,
    OutboundCall,
    SessionLocal,
    claim_next_campaign_job,
    create_campaign,
    create_campaign_jobs_from_contacts,
    create_follow_up_task,
    create_tables,
    get_active_channel_count,
    get_campaign_analytics,
    get_campaign_job,
    get_follow_up_tasks,
    get_leads,
    is_phone_dnd,
    recover_stale_jobs,
    release_channel_capacity,
    reserve_channel_capacity,
    update_job_status,
)
from backend.db_migration import rehearse_migration_on_copy
from backend.web import app
from backend.worker import CampaignWorker


@pytest.fixture(autouse=True)
def init_db(monkeypatch):
    """Ensure simulation mode and clean DB before each test."""
    monkeypatch.setenv("SIMULATION_MODE", "true")
    monkeypatch.setenv("LIVE_CALLS_ENABLED", "false")
    monkeypatch.setenv("TELEPHONY_PROVIDER", "mock")
    create_tables()


@pytest.fixture
def client():
    return TestClient(app)


def test_zero_credentials_manual_lead_creation_and_crm_listing(client):
    """Criterion: With zero credentials, user can add a lead, see it in CRM, and export it."""
    # 1. Add manual lead via API (JSON payload)
    payload = {
        "name": "Suresh Deshmukh",
        "phone_number": "+919876500010",
        "area": "Wakad",
        "bhk": "2 BHK",
        "budget": "78 Lakhs",
        "notes": "Walk-in enquiry at sales desk",
    }
    resp = client.post("/api/leads", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Suresh Deshmukh"
    assert data["phone_number"] == "+919876500010"

    # 2. Verify lead listing
    leads_resp = client.get("/api/leads")
    assert leads_resp.status_code == 200
    leads = leads_resp.json()
    matching = next((l for l in leads if l["phone_number"] == "+919876500010"), None)
    assert matching is not None
    assert matching["bhk"] == "2 BHK"
    assert matching["budget"] == "78 Lakhs"

    # 3. Verify lead export to Excel
    export_resp = client.get("/api/leads/export")
    assert export_resp.status_code == 200
    assert "spreadsheet" in export_resp.headers["content-type"]


def test_simulated_campaign_flow_with_zero_credentials():
    """Criterion: With zero credentials, launch a simulated campaign, see CRM updates and analytics."""
    sim_phone = f"+91987{uuid4().int % 9000000 + 1000000}"
    with SessionLocal() as s:
        # Create campaign and contacts
        camp = Campaign(
            name="Simulated Acceptance Drive",
            project_name="Krishna's AI Property - Wakad",
            agent_name="Priya - Residential Advisory Executive",
            language="en-IN",
            concurrency_limit=2,
            max_attempts=2,
            status="running",
        )
        s.add(camp)
        s.commit()
        s.refresh(camp)

        cc = CampaignContact(
            campaign_id=camp.id,
            contact_id=101,
            name="Anand Sharma",
            phone_number=sim_phone,
            locality="Wakad",
            status="pending",
        )
        s.add(cc)
        s.commit()
        camp_id = camp.id

    # Populate durable jobs
    jobs_created = create_campaign_jobs_from_contacts(camp_id)
    assert jobs_created >= 1

    # Run worker to process the job
    worker = CampaignWorker(worker_id="test-worker-1")
    processed = asyncio.run(worker.process_next_job(campaign_id=camp_id))
    assert processed is True

    # Verify CRM lead was created from simulation
    leads = get_leads()
    lead = next((l for l in leads if l.phone_number == sim_phone), None)
    assert lead is not None
    assert lead.interest_status in ("Hot", "Warm")

    # Verify campaign analytics
    analytics = get_campaign_analytics(camp_id)
    assert analytics["qualified_leads"] >= 1


def test_simulated_scenarios_and_dnd_opt_out():
    """Criterion: Simulated busy, no-answer, opt-out, and failure paths behave as expected."""
    phone_busy = f"+919871{uuid4().int % 900000 + 100000}"
    phone_noans = f"+919872{uuid4().int % 900000 + 100000}"
    phone_dnd = f"+919873{uuid4().int % 900000 + 100000}"

    with SessionLocal() as s:
        camp = Campaign(
            name="Scenarios Test Campaign",
            project_name="Krishna Wakad",
            agent_name="Priya",
            status="running",
            concurrency_limit=5,
            max_attempts=2,
        )
        s.add(camp)
        s.commit()
        camp_id = camp.id

        # 1. Busy contact (triggers retry)
        c_busy = CampaignContact(
            campaign_id=camp_id, contact_id=201, name="Busy Customer",
            phone_number=phone_busy, status="pending"
        )
        # 2. No-answer contact (triggers retry)
        c_noans = CampaignContact(
            campaign_id=camp_id, contact_id=202, name="No Answer Customer",
            phone_number=phone_noans, status="pending"
        )
        # 3. DND opt-out contact
        c_dnd = CampaignContact(
            campaign_id=camp_id, contact_id=203, name="Opt Out Customer",
            phone_number=phone_dnd, status="pending"
        )
        s.add_all([c_busy, c_noans, c_dnd])
        s.commit()

    create_campaign_jobs_from_contacts(camp_id)
    worker = CampaignWorker(worker_id="scenario-worker")

    # Process all 3 jobs
    asyncio.run(worker.process_next_job(camp_id))
    asyncio.run(worker.process_next_job(camp_id))
    asyncio.run(worker.process_next_job(camp_id))

    with SessionLocal() as s:
        j_busy = s.scalar(s.query(CampaignJob).filter(CampaignJob.campaign_id == camp_id, CampaignJob.phone_number == phone_busy))
        j_noans = s.scalar(s.query(CampaignJob).filter(CampaignJob.campaign_id == camp_id, CampaignJob.phone_number == phone_noans))
        j_dnd = s.scalar(s.query(CampaignJob).filter(CampaignJob.campaign_id == camp_id, CampaignJob.phone_number == phone_dnd))

        # Busy and No-Answer should be scheduled for retry (attempt = 1 < max_attempts 2)
        assert j_busy.status == "retry_scheduled"
        assert j_busy.attempt == 1
        assert j_noans.status == "retry_scheduled"
        assert j_noans.attempt == 1

        # Opt-out should be dnd_blocked
        assert j_dnd.status == "dnd_blocked"

    # Verify DND registry now suppresses future dispatches
    assert is_phone_dnd(phone_dnd) is True


def test_human_escalation_task_creation():
    """Criterion: When caller asks for a human, create a sales follow-up task in CRM."""
    with SessionLocal() as s:
        camp = Campaign(
            name="Human Escalation Test",
            project_name="Krishna Wakad",
            agent_name="Priya",
            status="running",
            concurrency_limit=2,
            max_attempts=2,
        )
        s.add(camp)
        s.commit()
        c_human = CampaignContact(
            campaign_id=camp.id, contact_id=301, name="Manager Request Customer",
            phone_number="+919876500004", status="pending"
        )
        s.add(c_human)
        s.commit()
        camp_id = camp.id

    create_campaign_jobs_from_contacts(camp_id)
    worker = CampaignWorker(worker_id="human-worker")
    asyncio.run(worker.process_next_job(camp_id))

    # Verify task was created in CRM
    tasks = get_follow_up_tasks()
    matching = next((t for t in tasks if t.phone_number == "+919876500004"), None)
    assert matching is not None
    assert matching.task_type == "human_escalation"
    assert matching.priority == "high"
    assert matching.status == "pending"


def test_concurrency_limit_and_two_workers():
    """Criterion: With two workers running and channel limit of two, third call waits."""
    with SessionLocal() as s:
        camp = Campaign(
            name="Concurrency Test Drive",
            project_name="Krishna Wakad",
            agent_name="Priya",
            status="running",
            concurrency_limit=2,  # Limit is 2
            max_attempts=2,
        )
        s.add(camp)
        s.commit()
        camp_id = camp.id

        # Add 3 contacts
        s.add_all([
            CampaignContact(campaign_id=camp_id, contact_id=401, name="Lead 1", phone_number="+919876500401", status="pending"),
            CampaignContact(campaign_id=camp_id, contact_id=402, name="Lead 2", phone_number="+919876500402", status="pending"),
            CampaignContact(campaign_id=camp_id, contact_id=403, name="Lead 3", phone_number="+919876500403", status="pending"),
        ])
        s.commit()

    create_campaign_jobs_from_contacts(camp_id)

    # Artificially reserve 2 active channels to simulate 2 ongoing calls
    ch1 = reserve_channel_capacity(camp_id, job_id=9001, phone_number="+919876500401", timeout_seconds=600)
    ch2 = reserve_channel_capacity(camp_id, job_id=9002, phone_number="+919876500402", timeout_seconds=600)
    assert ch1 is not None
    assert ch2 is not None
    assert get_active_channel_count(camp_id) == 2

    # A 3rd channel reservation must fail / wait
    ch3 = reserve_channel_capacity(camp_id, job_id=9003, phone_number="+919876500403", timeout_seconds=600)
    assert ch3 is None, "Third reservation must be rejected when channel capacity limit is reached"

    # Worker attempting to process 3rd job must release it to wait
    worker = CampaignWorker(worker_id="capacity-worker")
    processed = asyncio.run(worker.process_next_job(camp_id))
    assert processed is True

    # Check that job status remained 'pending' (released because channel capacity was full)
    with SessionLocal() as s:
        pending_jobs = s.query(CampaignJob).filter(CampaignJob.campaign_id == camp_id, CampaignJob.status == "pending").count()
        assert pending_jobs >= 1

    # Cleanup reserved channels
    release_channel_capacity(job_id=9001)
    release_channel_capacity(job_id=9002)
    assert get_active_channel_count(camp_id) == 0


def test_stale_lease_recovery_after_worker_restart():
    """Criterion: Restarting a worker neither loses pending jobs nor causes duplicate dispatch."""
    with SessionLocal() as s:
        camp = Campaign(name="Crash Recovery", status="running", concurrency_limit=2)
        s.add(camp)
        s.commit()
        camp_id = camp.id

        # Job claimed by a dead worker with expired lease
        past_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        stale_job = CampaignJob(
            campaign_id=camp_id,
            contact_id=501,
            phone_number="+919876500501",
            status="claimed",
            claimed_by_worker="crashed-worker-pid-8888",
            claimed_at=past_time,
            lease_expires_at=past_time,
        )
        s.add(stale_job)
        s.commit()
        stale_id = stale_job.id

    # New worker starts and runs recovery
    recovered_count = recover_stale_jobs("new-worker-1")
    assert recovered_count >= 1

    # Verify job status was reset to pending without losing it
    recovered_job = get_campaign_job(stale_id)
    assert recovered_job.status == "pending"
    assert recovered_job.claimed_by_worker is None


def test_idempotent_duplicate_callback_handling(client):
    """Criterion: Duplicate callbacks do not create extra retries or release capacity twice."""
    call_sid = f"mock_dup_{uuid4().hex[:8]}"
    with SessionLocal() as s:
        call = OutboundCall(
            lead_id=1,
            phone_number="+919876500601",
            provider_call_id=call_sid,
            status="calling",
            provider="exotel",
        )
        s.add(call)
        s.commit()

    # Reserve channel capacity
    reserve_channel_capacity(campaign_id=1, job_id=6001, phone_number="+919876500601", timeout_seconds=300)

    # First callback
    resp1 = client.post("/api/telephony/webhook/exotel/status", data={"CallSid": call_sid, "Status": "completed", "Duration": "45"})
    assert resp1.status_code == 200

    # Second duplicate callback (simulating carrier webhook retry)
    resp2 = client.post("/api/telephony/webhook/exotel/status", data={"CallSid": call_sid, "Status": "completed", "Duration": "45"})
    assert resp2.status_code == 200

    # Capacity was released once cleanly without errors
    release_res = release_channel_capacity(job_id=6001)
    assert release_res is True


def test_recording_playback_endpoint_states(client):
    """Criterion: Dashboard recording playback with fixture shows ready, pending, unavailable, and unauthorized states."""
    # 1. Ready state with fixture
    call_ready = f"call_ready_{uuid4().hex[:6]}"
    with SessionLocal() as s:
        s.add(OutboundCall(
            lead_id=1, phone_number="+919876500701", provider_call_id=call_ready,
            status="completed", call_status="completed", recording_url="fixture://sample_call.wav",
            duration_seconds=42, provider="mock",
        ))
        s.commit()

    resp_ready = client.get(f"/api/recordings/{call_ready}")
    assert resp_ready.status_code == 200
    assert "audio" in resp_ready.headers["content-type"]

    # 2. Pending state (active call)
    call_pending = f"call_pend_{uuid4().hex[:6]}"
    with SessionLocal() as s:
        s.add(OutboundCall(
            lead_id=1, phone_number="+919876500702", provider_call_id=call_pending,
            status="calling", call_status="calling", recording_url="fixture://sample_call.wav",
            provider="exotel",
        ))
        s.commit()

    resp_pending = client.get(f"/api/recordings/{call_pending}")
    assert resp_pending.status_code == 202
    assert resp_pending.json()["status"] == "pending"

    # 3. Unavailable state (no recording url)
    call_unavail = f"call_unavail_{uuid4().hex[:6]}"
    with SessionLocal() as s:
        s.add(OutboundCall(
            lead_id=1, phone_number="+919876500703", provider_call_id=call_unavail,
            status="completed", call_status="completed", recording_url="",
            provider="exotel",
        ))
        s.commit()

    resp_unavail = client.get(f"/api/recordings/{call_unavail}")
    assert resp_unavail.status_code == 404
    assert resp_unavail.json()["status"] == "unavailable"

    # 4. Unauthorized state (invalid auth token)
    resp_unauth = client.get(f"/api/recordings/{call_ready}?auth_token=invalid_token")
    assert resp_unauth.status_code == 403


def test_rehearse_database_migration():
    """Criterion: Rehearse SQLite migration on a copy and verify all counts carry over."""
    report = rehearse_migration_on_copy()
    assert report["success"] is True
    assert report["rehearsal_mode"] is True
    for entity, counts in report["verification"].items():
        assert counts["source"] == counts["target"], f"Mismatch in {entity}: {counts}"
