"""Automated Test Suite for Telephony Integration and Webhook Processing."""

import base64
import os
from unittest.mock import patch
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient

from backend.crm import (
    create_tables,
    create_or_update_lead,
    get_outbound_calls,
    save_outbound_call,
    update_outbound_call_status,
)
from backend.telephony import (
    CallRequest,
    CallResult,
    CallStatusUpdate,
    get_configured_provider_name,
    get_provider,
    get_telephony_status,
    initiate_outbound_call,
    process_webhook,
    validate_phone_number,
)
from backend.telephony.exotel import ExotelProvider
from backend.telephony.mock import MockProvider
from backend.telephony.twilio import TwilioProvider
from backend.web import app


@pytest.fixture(autouse=True)
def setup_db():
    create_tables()


@pytest.fixture
def client():
    return TestClient(app)


def test_phone_number_validation():
    """Verify strict E.164 and Indian mobile number validation."""
    # Valid Indian formats
    assert validate_phone_number("9876543210") == "+919876543210"
    assert validate_phone_number("+919876543210") == "+919876543210"
    assert validate_phone_number("09876543210") == "+919876543210"
    assert validate_phone_number("+91 98765 43210") == "+919876543210"
    assert validate_phone_number("91-9876543210") == "+919876543210"

    # Invalid formats
    with pytest.raises(ValueError, match="Invalid phone number"):
        validate_phone_number("12345")

    with pytest.raises(ValueError, match="Invalid phone number"):
        validate_phone_number("invalid-phone")


def test_mock_provider_lifecycle():
    """Verify mock provider simulation lifecycle."""
    provider = MockProvider()
    ready, msg, missing = provider.is_ready()
    assert ready is True
    assert missing == []

    req = CallRequest(
        phone_number="9876543210",
        customer_name="Vikram Joshi",
        campaign_id=1,
    )
    result = provider.initiate_call(req)
    assert result.success is True
    assert result.provider == "mock"
    assert result.provider_call_id.startswith("mock-")
    assert result.status == "simulated_accepted"


def test_exotel_provider_readiness_and_config():
    """Verify Exotel readiness checks and missing configuration reporting."""
    provider = ExotelProvider()
    with patch.dict(os.environ, {}, clear=True):
        ready, msg, missing = provider.is_ready()
        assert ready is False
        assert "EXOTEL_API_KEY" in missing
        assert "EXOTEL_ACCOUNT_SID" in missing
        assert "EXOTEL_CALLER_ID" in missing

    with patch.dict(os.environ, {
        "EXOTEL_ACCOUNT_SID": "test_sid",
        "EXOTEL_API_KEY": "test_key",
        "EXOTEL_API_TOKEN": "test_token",
        "EXOTEL_CALLER_ID": "08012345678",
    }):
        ready, msg, missing = provider.is_ready()
        assert ready is True
        assert missing == []


def test_twilio_provider_readiness_and_config():
    """Verify Twilio readiness checks and missing configuration reporting."""
    provider = TwilioProvider()
    with patch.dict(os.environ, {}, clear=True):
        ready, msg, missing = provider.is_ready()
        assert ready is False
        assert "TWILIO_ACCOUNT_SID" in missing
        assert "TWILIO_AUTH_TOKEN" in missing
        assert "TWILIO_PHONE_NUMBER" in missing

    with patch.dict(os.environ, {
        "TWILIO_ACCOUNT_SID": "AC_test_sid",
        "TWILIO_AUTH_TOKEN": "test_auth_token",
        "TWILIO_PHONE_NUMBER": "+12025550199",
    }):
        ready, msg, missing = provider.is_ready()
        assert ready is True
        assert missing == []


def test_exotel_status_callback_parsing():
    """Verify Exotel webhook status callback parsing."""
    provider = ExotelProvider()
    payload = {
        "CallSid": "exo_call_12345",
        "Status": "completed",
        "DialCallDuration": "45",
        "RecordingUrl": "https://s3.amazonaws.com/exo_rec_12345.mp3",
    }
    update = provider.parse_status_callback(payload)
    assert update.provider == "exotel"
    assert update.provider_call_id == "exo_call_12345"
    assert update.status == "completed"
    assert update.duration_seconds == 45
    assert update.recording_url == "https://s3.amazonaws.com/exo_rec_12345.mp3"


def test_twilio_status_callback_parsing():
    """Verify Twilio webhook status callback parsing."""
    provider = TwilioProvider()
    payload = {
        "CallSid": "CA_twilio_call_9999",
        "CallStatus": "in-progress",
        "CallDuration": "0",
    }
    update = provider.parse_status_callback(payload)
    assert update.provider == "twilio"
    assert update.provider_call_id == "CA_twilio_call_9999"
    assert update.status == "in_progress"


def test_outbound_call_database_tracking():
    """Verify database persistence and state transitions for outbound calls."""
    lead = create_or_update_lead(
        name="Pooja Sharma",
        phone_number="9876500001",
        area="Wakad",
    )

    call_sid = f"test_sid_{uuid4().hex[:8]}"
    call = save_outbound_call(
        lead_id=lead.id,
        phone_number="+919876500001",
        provider_call_id=call_sid,
        status="initiated",
        provider="exotel",
    )
    assert call.id is not None
    assert call.call_status == "initiated"

    # Update status to answered / in_progress
    updated = update_outbound_call_status(
        provider_call_id=call_sid,
        status="answered",
    )
    assert updated is not None
    assert updated.call_status == "answered"
    assert updated.answered_at is not None

    # Update status to completed
    final = update_outbound_call_status(
        provider_call_id=call_sid,
        status="completed",
        duration_seconds=55,
        recording_url="https://storage.exotel.com/rec001.mp3",
    )
    assert final.call_status == "completed"
    assert final.duration_seconds == 55
    assert final.recording_url == "https://storage.exotel.com/rec001.mp3"
    assert final.ended_at is not None


def test_api_telephony_status_endpoint(client):
    """Verify /api/telephony/status inspects provider readiness."""
    resp = client.get("/api/telephony/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "active_provider" in data
    assert "supported_providers" in data
    assert "exotel" in data["supported_providers"]
    assert "twilio" in data["supported_providers"]
    assert "mock" in data["supported_providers"]


def test_api_telephony_call_validation_and_dnd(client):
    """Verify /api/telephony/call checks lead existence, phone format, and DND suppression."""
    # Non-existent lead
    resp = client.post("/api/telephony/call", json={"lead_id": 999999})
    assert resp.status_code == 404

    # DND opted out lead
    opt_out_lead = create_or_update_lead(
        name="DND Customer",
        phone_number="9876599999",
        opt_out=True,
    )
    resp = client.post("/api/telephony/call", json={"lead_id": opt_out_lead.id})
    assert resp.status_code == 409
    assert "Do Not Call" in resp.json()["detail"]


def test_api_telephony_exotel_webhook(client):
    """Verify Exotel webhook status callback endpoint updates call records."""
    exo_sid = f"exo_web_{uuid4().hex[:8]}"
    call = save_outbound_call(
        lead_id=None,
        phone_number="+919876543211",
        provider_call_id=exo_sid,
        status="ringing",
        provider="exotel",
    )

    resp = client.post(
        "/api/telephony/webhook/exotel/status",
        data={
            "CallSid": exo_sid,
            "Status": "completed",
            "DialCallDuration": "60",
            "RecordingUrl": "https://rec.exotel.com/123.mp3",
        },
    )
    assert resp.status_code == 200

    calls = get_outbound_calls()
    matched = [c for c in calls if c.provider_call_id == exo_sid]
    assert len(matched) == 1
    assert matched[0].call_status == "completed"
    assert matched[0].duration_seconds == 60


def test_api_telephony_twilio_webhook(client):
    """Verify Twilio webhook status callback endpoint updates call records."""
    twilio_sid = f"CA_web_{uuid4().hex[:8]}"
    call = save_outbound_call(
        lead_id=None,
        phone_number="+919876543212",
        provider_call_id=twilio_sid,
        status="initiated",
        provider="twilio",
    )

    resp = client.post(
        "/api/telephony/webhook/twilio/status",
        data={
            "CallSid": twilio_sid,
            "CallStatus": "completed",
            "CallDuration": "82",
        },
    )
    assert resp.status_code == 200

    calls = get_outbound_calls()
    matched = [c for c in calls if c.provider_call_id == twilio_sid]
    assert len(matched) == 1
    assert matched[0].call_status == "completed"
    assert matched[0].duration_seconds == 82


def test_api_exotel_voicebot_endpoint(client):
    """Verify Exotel Voicebot Applet dynamic URL resolution returns wss:// stream endpoint."""
    resp = client.get("/api/telephony/exotel/voicebot-endpoint")
    assert resp.status_code == 200
    data = resp.json()
    assert "url" in data
    assert "/ws/telephony/exotel/stream" in data["url"]

    # Also test POST method as Exotel flows can invoke GET or POST
    resp_post = client.post("/api/telephony/exotel/voicebot-endpoint")
    assert resp_post.status_code == 200
    assert "/ws/telephony/exotel/stream" in resp_post.json()["url"]


def test_exotel_initiate_call_payload():
    """Verify Exotel initiate_call uses Applet ExoML URL when EXOTEL_APPLET_ID is set, and voicebot fallback otherwise."""
    provider = ExotelProvider()
    base_env = {
        "EXOTEL_ACCOUNT_SID": "acc_12345",
        "EXOTEL_API_KEY": "key_12345",
        "EXOTEL_API_TOKEN": "token_12345",
        "EXOTEL_CALLER_ID": "08047190000",
        "AGENTSTREAM_PUBLIC_URL": "https://voice.sample.app",
    }
    req = CallRequest(
        phone_number="9876543210",
        lead_id=42,
        campaign_id=7,
        customer_name="Aditi Rao",
    )

    # 1. With EXOTEL_APPLET_ID set: must route through Exotel App Bazaar ExoML flow URL
    with patch.dict(os.environ, {**base_env, "EXOTEL_APPLET_ID": "1373255"}):
        with patch("httpx.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                "Call": {
                    "Sid": "exo_call_99999",
                    "Status": "queued",
                    "DateCreated": "2026-11-01 20:00:00",
                }
            }

            result = provider.initiate_call(req)
            assert result.success is True
            assert result.provider == "exotel"
            assert result.provider_call_id == "exo_call_99999"

            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args.kwargs
            assert call_kwargs["auth"] == ("key_12345", "token_12345")
            form_data = call_kwargs["data"]
            assert form_data["From"] == "09876543210"
            assert form_data["CallerId"] == "08047190000"
            assert form_data["CallType"] == "trans"
            assert form_data["Url"] == "https://my.exotel.com/acc_12345/exoml/start_voice/1373255"
            assert form_data["StatusCallback"] == "https://voice.sample.app/api/telephony/webhook/exotel/status"

    # 2. Without EXOTEL_APPLET_ID: falls back to voicebot-endpoint
    with patch.dict(os.environ, {**base_env, "EXOTEL_APPLET_ID": ""}):
        with patch("httpx.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                "Call": {"Sid": "exo_call_fallback", "Status": "queued"}
            }
            provider.initiate_call(req)
            form_data = mock_post.call_args.kwargs["data"]
            assert "https://voice.sample.app/api/telephony/exotel/voicebot-endpoint" in form_data["Url"]


def test_exotel_webhook_basic_auth_verification():
    """Verify Basic Auth signature verification on incoming Exotel webhooks."""
    provider = ExotelProvider()
    import base64
    valid_creds = base64.b64encode(b"test_key:test_token").decode("ascii")

    with patch.dict(os.environ, {
        "EXOTEL_ACCOUNT_SID": "test_sid",
        "EXOTEL_API_KEY": "test_key",
        "EXOTEL_API_TOKEN": "test_token",
    }):
        # Valid Basic Auth
        assert provider.verify_webhook_signature(
            {"authorization": f"Basic {valid_creds}"}, b""
        ) is True

        # Invalid Basic Auth
        invalid_creds = base64.b64encode(b"test_key:wrong_token").decode("ascii")
        assert provider.verify_webhook_signature(
            {"authorization": f"Basic {invalid_creds}"}, b""
        ) is False

        # Custom header token
        assert provider.verify_webhook_signature(
            {"x-exotel-token": "test_token"}, b""
        ) is True


def test_exotel_agentstream_websocket_greeting_and_flow(client):
    """Verify Exotel AgentStream WebSocket handles start, delivers s16le PCM greeting, and completes call."""
    import base64
    call_sid = f"exo_ws_{uuid4().hex[:8]}"

    # Pre-register outbound call record in DB
    save_outbound_call(
        lead_id=None,
        phone_number="+919876543210",
        provider_call_id=call_sid,
        status="initiated",
        provider="exotel",
    )

    # Mock synthesize_bytes to return fast dummy 8kHz s16le audio
    with patch("backend.web.tts.synthesize_bytes") as mock_synth:
        # Return 3200 bytes of s16le PCM (0.2s of audio)
        mock_synth.return_value = b"\x10\x00" * 1600

        with client.websocket_connect("/ws/telephony/exotel/stream") as ws:
            # 1. Send start event matching Exotel AgentStream specification
            ws.send_json({
                "event": "start",
                "stream_sid": "stream_exo_001",
                "start": {
                    "account_sid": "acc_test",
                    "call_sid": call_sid,
                    "stream_sid": "stream_exo_001",
                    "custom_parameters": {
                        "customer_name": "Rohan Deshmukh",
                    },
                    "media_format": {
                        "encoding": "audio/x-raw",
                        "format": "s16le",
                        "sample_rate": 8000,
                        "channels": 1,
                    },
                },
            })

            # 2. Expect at least one media frame containing s16le audio greeting
            received_msg = ws.receive_json()
            assert received_msg["event"] == "media"
            assert received_msg["stream_sid"] == "stream_exo_001"
            assert "payload" in received_msg["media"]
            raw_pcm = base64.b64decode(received_msg["media"]["payload"])
            assert len(raw_pcm) > 0

            # 3. Send stop event
            ws.send_json({
                "event": "stop",
                "stream_sid": "stream_exo_001",
            })

    # Verify call status was updated to completed in DB
    calls = get_outbound_calls()
    matched = [c for c in calls if c.provider_call_id == call_sid]
    assert len(matched) == 1
    assert matched[0].call_status == "completed"


def test_sarvam_speech_pipeline_buffers():
    """Verify SpeechToText and TextToSpeech work with in-memory buffers."""
    from backend.speech.stt import SpeechToText
    from backend.speech.tts import TextToSpeech
    import numpy as np

    stt = SpeechToText()
    tts = TextToSpeech()

    # Empty buffer handling
    assert stt.transcribe_pcm(b"")["text"] == ""

    # Mock Sarvam client convert and transcribe
    dummy_audio_bytes = np.zeros(1600, dtype=np.int16).tobytes()
    b64_dummy = base64.b64encode(dummy_audio_bytes).decode("ascii")

    with patch.object(tts.client.text_to_speech, "convert") as mock_convert:
        class DummyTTSResp:
            audios = [b64_dummy]
        mock_convert.return_value = DummyTTSResp()

        res_bytes = tts.synthesize_bytes("Hello", language_code="en-IN", sample_rate=8000, codec="linear16")
        assert res_bytes == dummy_audio_bytes

    with patch.object(stt.client.speech_to_text, "transcribe") as mock_transcribe:
        class DummySTTResp:
            transcript = "Looking for 2 BHK"
            language_code = "en-IN"
        mock_transcribe.return_value = DummySTTResp()

        res_dict = stt.transcribe_pcm(dummy_audio_bytes, sample_rate=16000)
        assert res_dict["text"] == "Looking for 2 BHK"
        assert res_dict["language"] == "en-IN"


def test_inbound_exotel_voicebot_dynamic_endpoint(client):
    """Verify Exotel Voicebot dynamic endpoint parses inbound caller query params, creates lead, and returns wss:// URL."""
    exo_sid = f"exo_in_{uuid4().hex[:8]}"
    resp = client.get(
        "/api/telephony/exotel/voicebot-endpoint",
        params={
            "CallSid": exo_sid,
            "From": "9876543210",
            "To": "09513886363",
            "Direction": "incoming",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "url" in data
    assert "/ws/telephony/exotel/stream" in data["url"]
    assert f"call_sid={exo_sid}" in data["url"]
    assert "direction=inbound" in data["url"]

    # Verify call record was created
    calls = get_outbound_calls()
    matched = [c for c in calls if c.provider_call_id == exo_sid]
    assert len(matched) == 1
    assert matched[0].lead_id > 0


def test_inbound_exotel_passthru_endpoint(client):
    """Verify Exotel Passthru Applet endpoint registers caller before stream."""
    call_sid = f"exo_pass_{uuid4().hex[:8]}"
    resp = client.post(
        "/api/telephony/exotel/passthru",
        data={
            "CallSid": call_sid,
            "From": "9876543222",
            "To": "09513886363",
        },
    )
    assert resp.status_code == 200
    assert resp.text == "OK"

    calls = get_outbound_calls()
    matched = [c for c in calls if c.provider_call_id == call_sid]
    assert len(matched) == 1


def test_inbound_twilio_voice_webhook(client):
    """Verify Twilio Inbound Voice Webhook returns TwiML with Stream and registers caller."""
    tw_sid = f"CA_in_{uuid4().hex[:8]}"
    resp = client.post(
        "/api/telephony/twilio/inbound",
        data={
            "CallSid": tw_sid,
            "From": "+919876543233",
            "To": "+12025550199",
        },
    )
    assert resp.status_code == 200
    assert "application/xml" in resp.headers["content-type"]
    assert "<Connect>" in resp.text
    assert "<Stream" in resp.text
    assert 'name="direction" value="inbound"' in resp.text

    calls = get_outbound_calls()
    matched = [c for c in calls if c.provider_call_id == tw_sid]
    assert len(matched) == 1


def test_inbound_call_simulation(client):
    """Verify end-to-end inbound call simulation endpoint."""
    resp = client.post(
        "/api/telephony/inbound/simulate",
        json={
            "caller_phone": "9876543244",
            "customer_speech": "Hello, I want details on 2 BHK flats in Wakad under 75 lakhs.",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert "Thank you for calling" in data["greeting"]
    assert "call_summary_id" in data
    assert data["lead_id"] > 0


def test_inbound_exotel_agentstream_websocket_greeting(client):
    """Verify Exotel AgentStream delivers an INBOUND greeting ('Thank you for calling') and creates lead."""
    call_sid = f"exo_in_ws_{uuid4().hex[:8]}"

    with patch("backend.web.tts.synthesize_bytes") as mock_synth:
        mock_synth.return_value = b"\x10\x00" * 1600

        with client.websocket_connect(f"/ws/telephony/exotel/stream?call_sid={call_sid}&from=9876543255&direction=inbound") as ws:
            ws.send_json({
                "event": "start",
                "stream_sid": "stream_in_exo_99",
                "start": {
                    "account_sid": "acc_test",
                    "call_sid": call_sid,
                    "stream_sid": "stream_in_exo_99",
                    "direction": "inbound",
                    "from": "+919876543255",
                    "to": "09513886363",
                    "media_format": {
                        "encoding": "audio/x-raw",
                        "format": "s16le",
                        "sample_rate": 8000,
                        "channels": 1,
                    },
                },
            })

            received_msg = ws.receive_json()
            assert received_msg["event"] == "media"

            # Check greeting synthesized
            mock_synth.assert_called()
            synthesized_text = mock_synth.call_args.args[0] if mock_synth.call_args.args else mock_synth.call_args.kwargs.get("text", "")
            assert "Thank you for calling" in synthesized_text

            ws.send_json({
                "event": "stop",
                "stream_sid": "stream_in_exo_99",
            })

    calls = get_outbound_calls()
    matched = [c for c in calls if c.provider_call_id == call_sid]
    assert len(matched) == 1
    assert matched[0].call_status == "completed"


def test_outbound_exotel_voicebot_dynamic_endpoint(client):
    """Verify Exotel Voicebot endpoint detects outbound calls by CallSid, encodes phone, and avoids inbound lead creation."""
    from urllib.parse import urlparse, parse_qs
    from backend.crm import get_leads, get_outbound_call_by_sid

    suffix = uuid4().hex[:6]
    test_phone = f"+919876{int(suffix, 16) % 1000000:06d}"
    lead = create_or_update_lead(name="Outbound Test Prospect", phone_number=test_phone)
    call_sid = f"exo_out_{uuid4().hex[:8]}"

    save_outbound_call(
        lead_id=lead.id,
        phone_number=test_phone,
        provider_call_id=call_sid,
        status="queued",
        message=f"Exotel call initiated successfully (SID: {call_sid}, Status: queued)",
        provider="exotel",
    )

    lead_count_before = len(get_leads())

    # On outbound calls, Exotel often passes ExoPhone as From and customer or empty as To
    resp = client.get(
        "/api/telephony/exotel/voicebot-endpoint",
        params={
            "CallSid": call_sid,
            "From": "09513886363",
            "To": test_phone,
        },
    )
    assert resp.status_code == 200
    ws_url = resp.json()["url"]
    parsed = urlparse(ws_url)
    qs = parse_qs(parsed.query)

    assert qs["call_sid"] == [call_sid]
    assert qs["direction"] == ["outbound"]
    assert qs["from"] == [test_phone]
    assert qs["lead_id"] == [str(lead.id)]
    assert get_outbound_call_by_sid(call_sid) is not None
    # Must NOT have created a bogus Inbound Caller lead for 09513886363
    assert len(get_leads()) == lead_count_before


def test_exotel_pcm_chunking_and_wav_strip():
    """Verify WAV header stripping, 3200-byte Exotel chunk alignment, and sample rate resampling."""
    import io
    import wave
    from backend.web import _prepare_exotel_pcm_chunks, _resample_pcm_to_16k

    # 1. Raw PCM with unaligned length -> padded into 3200-byte chunks (all multiples of 320)
    raw_pcm = b"\x01\x02" * 2000  # 4000 bytes
    chunks = _prepare_exotel_pcm_chunks(raw_pcm, chunk_size=3200)
    assert len(chunks) == 2
    assert all(len(c) == 3200 for c in chunks)
    assert all(len(c) % 320 == 0 for c in chunks)
    assert chunks[0] == raw_pcm[:3200]
    assert chunks[1].startswith(raw_pcm[3200:])

    # 2. WAV container input -> RIFF header stripped cleanly
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x05\x00" * 1600)  # 3200 raw bytes
    wav_chunks = _prepare_exotel_pcm_chunks(wav_buf.getvalue(), chunk_size=3200)
    assert len(wav_chunks) == 1
    assert not wav_chunks[0].startswith(b"RIFF")
    assert wav_chunks[0] == b"\x05\x00" * 1600

    # 3. Resampling 8kHz -> 16kHz doubles bytes, 16kHz -> pass-through
    pcm_8k = b"\x10\x00" * 160
    assert len(_resample_pcm_to_16k(pcm_8k, 8000)) == len(pcm_8k) * 2
    assert _resample_pcm_to_16k(pcm_8k, 16000) == pcm_8k


def test_outbound_exotel_full_lifecycle_websocket_to_crm(client):
    """End-to-end outbound call lifecycle: WebSocket -> 2-way audio (20ms VAD) -> LLM/TTS -> CRM & CampaignContact persistence."""
    import struct
    from backend.crm import (
        SessionLocal,
        CallSummary,
        Appointment,
        CampaignContact,
        create_campaign,
        get_lead_by_phone,
        get_outbound_call_by_sid,
        update_campaign_contact_status,
        select,
    )

    suffix = uuid4().hex[:6]
    customer_phone = f"+919765{int(suffix, 16) % 1000000:06d}"
    call_sid = f"exo_out_ws_{uuid4().hex[:8]}"

    # 1. Create a campaign & campaign contact in 'calling' state (like CampaignEngine does)
    campaign = create_campaign(
        name=f"Pilot Test {suffix}",
        project_name="Krishna Property - Wakad",
        objective="Site Visit Booking",
        batch_id=None,
        agent_name="Priya",
        language="en-IN",
        window_start="09:30",
        window_end="19:30",
        max_attempts=3,
        retry_interval_minutes=60,
        assigned_sales_team="Sales Team",
        follow_up_policy="standard",
    )
    with SessionLocal() as db:
        cc = CampaignContact(
            campaign_id=campaign.id,
            contact_id=1,
            name="Vikram Joshi",
            phone_number=customer_phone,
            locality="Wakad",
            status="pending",
        )
        db.add(cc)
        db.commit()
        db.refresh(cc)
        cc_id = cc.id
    update_campaign_contact_status(cc_id, status="calling", session_id=call_sid)

    # Pre-register outbound call (campaign mode: lead_id=None)
    save_outbound_call(
        lead_id=None,
        phone_number=customer_phone,
        provider_call_id=call_sid,
        status="queued",
        message=f"Exotel call initiated successfully (SID: {call_sid}, Status: queued)",
        provider="exotel",
        campaign_id=campaign.id,
    )

    # Active speech PCM (20ms = 320 bytes @ 8kHz, amplitude 2000 -> RMS > 300) and silence PCM
    speech_frame_8k = struct.pack("<160h", *([2000] * 160))
    silence_frame_8k = b"\x00" * 320

    with patch("backend.web.tts.synthesize_bytes") as mock_synth, \
         patch("backend.web.stt.transcribe_pcm") as mock_stt, \
         patch("backend.web.conversation_manager._generate_llm_response") as mock_llm:

        # Return 4000 bytes to test unaligned padding into two 3200-byte Exotel frames
        mock_synth.return_value = b"\x10\x00" * 2000
        mock_stt.return_value = {
            "text": "Yes, I am looking for a 2 BHK flat in Wakad around 75 lakhs. Can I visit this Saturday at 11 AM?",
            "language": "en-IN",
        }
        mock_llm.return_value = "Sure Vikram, our 2 BHK starts at 72 Lakhs. Shall I book your Saturday 11 AM visit?"

        with client.websocket_connect(
            f"/ws/telephony/exotel/stream?call_sid={call_sid}&from={customer_phone.replace('+', '%2B')}&direction=outbound"
        ) as ws:
            ws.send_json({
                "event": "start",
                "stream_sid": "stream_out_01",
                "start": {
                    "account_sid": "acc_test",
                    "call_sid": call_sid,
                    "stream_sid": "stream_out_01",
                    "from": "09513886363",
                    "to": customer_phone,
                    "media_format": {
                        "encoding": "audio/x-raw",
                        "format": "s16le",
                        "sample_rate": 8000,
                        "channels": 1,
                    },
                },
            })

            # Receive opening greeting frames (2 chunks of 3200 bytes)
            greet_frame_1 = ws.receive_json()
            greet_frame_2 = ws.receive_json()
            assert greet_frame_1["event"] == "media"
            assert len(base64.b64decode(greet_frame_1["media"]["payload"])) == 3200
            assert len(base64.b64decode(greet_frame_2["media"]["payload"])) == 3200

            # Verify OUTBOUND greeting was generated (not inbound 'Thank you for calling')
            greeting_text = mock_synth.call_args_list[0].kwargs.get("text") or mock_synth.call_args_list[0].args[0]
            assert "Thank you for calling" not in greeting_text
            assert "calling regarding" in greeting_text

            # Send 15 speech frames (300ms -> 9600 bytes @ 16kHz >= 6400 threshold)
            for _ in range(15):
                ws.send_json({
                    "event": "media",
                    "stream_sid": "stream_out_01",
                    "media": {"payload": base64.b64encode(speech_frame_8k).decode("ascii")},
                })

            # Expect barge-in clear event emitted once on speech start
            clear_msg = ws.receive_json()
            assert clear_msg["event"] == "clear"
            assert clear_msg["stream_sid"] == "stream_out_01"

            # Send 26 silence frames (520ms -> 16640 bytes @ 16kHz >= 16000 threshold)
            for _ in range(26):
                ws.send_json({
                    "event": "media",
                    "stream_sid": "stream_out_01",
                    "media": {"payload": base64.b64encode(silence_frame_8k).decode("ascii")},
                })

            # Receive 2 reply audio frames
            reply_frame_1 = ws.receive_json()
            reply_frame_2 = ws.receive_json()
            assert reply_frame_1["event"] == "media"
            assert len(base64.b64decode(reply_frame_1["media"]["payload"])) == 3200
            assert len(base64.b64decode(reply_frame_2["media"]["payload"])) == 3200

            ws.send_json({"event": "stop", "stream_sid": "stream_out_01"})

    # Verify full CRM persistence
    call_rec = get_outbound_call_by_sid(call_sid)
    assert call_rec is not None
    assert call_rec.call_status == "completed"

    lead_rec = get_lead_by_phone(customer_phone)
    assert lead_rec is not None
    assert lead_rec.score > 0

    with SessionLocal() as db:
        cs = db.scalar(select(CallSummary).where(CallSummary.session_id == call_sid))
        assert cs is not None
        assert cs.call_mode == "telephony_live"
        assert "2 BHK" in cs.transcript

        appt = db.scalar(select(Appointment).where(Appointment.lead_id == lead_rec.id))
        assert appt is not None

        cc_after = db.get(CampaignContact, cc_id)
        assert cc_after.status == "completed"
        assert cc_after.lead_id == lead_rec.id
        assert cc_after.call_outcome == cs.interest_status


def test_outbound_exotel_call_concluded_terminates_call(client):
    """Verify that when customer declines (is_concluded=True), bot terminates Exotel call after goodbye."""
    import struct
    call_sid = f"exo_end_{uuid4().hex[:8]}"
    customer_phone = f"+919654{int(uuid4().hex[:6], 16) % 1000000:06d}"

    save_outbound_call(
        lead_id=None,
        phone_number=customer_phone,
        provider_call_id=call_sid,
        status="queued",
        message="Exotel call initiated",
        provider="exotel",
    )

    speech_frame_8k = struct.pack("<160h", *([2000] * 160))
    silence_frame_8k = b"\x00" * 320

    with patch("backend.web.tts.synthesize_bytes", return_value=b"\x10\x00" * 1600), \
         patch("backend.web.stt.transcribe_pcm", return_value={"text": "No thank you, not interested.", "language": "en-IN"}), \
         patch("backend.telephony.exotel.ExotelProvider.terminate_call", return_value=True) as mock_term:

        with client.websocket_connect(f"/ws/telephony/exotel/stream?call_sid={call_sid}&direction=outbound") as ws:
            ws.send_json({
                "event": "start",
                "stream_sid": "stream_end_01",
                "start": {"call_sid": call_sid, "stream_sid": "stream_end_01"},
            })
            assert ws.receive_json()["event"] == "media"

            for _ in range(15):
                ws.send_json({"event": "media", "stream_sid": "stream_end_01", "media": {"payload": base64.b64encode(speech_frame_8k).decode("ascii")}})
            assert ws.receive_json()["event"] == "clear"

            for _ in range(26):
                ws.send_json({"event": "media", "stream_sid": "stream_end_01", "media": {"payload": base64.b64encode(silence_frame_8k).decode("ascii")}})
            assert ws.receive_json()["event"] == "media"

            # Wait for server to run sleep(0.5), call terminate_call, and close the WebSocket
            with pytest.raises(Exception):
                ws.receive_json()

        mock_term.assert_called_once_with(call_sid)


def test_exotel_status_webhook_updates_campaign_contact(client):
    """Verify Exotel terminal status webhook transitions CampaignContact out of calling status."""
    from backend.crm import (
        SessionLocal,
        CampaignContact,
        create_campaign,
        get_outbound_call_by_sid,
        update_campaign_contact_status,
    )

    call_sid = f"exo_wh_{uuid4().hex[:8]}"
    campaign = create_campaign(
        name=f"Webhook Sync {uuid4().hex[:4]}",
        project_name="Krishna Property - Wakad",
        objective="Site Visit Booking",
        batch_id=None,
        agent_name="Priya",
        language="en-IN",
        window_start="09:30",
        window_end="19:30",
        max_attempts=3,
        retry_interval_minutes=60,
        assigned_sales_team="Sales Team",
        follow_up_policy="standard",
    )
    with SessionLocal() as db:
        cc = CampaignContact(
            campaign_id=campaign.id,
            contact_id=1,
            name="Test User",
            phone_number="+919876001122",
            status="pending",
        )
        db.add(cc)
        db.commit()
        db.refresh(cc)
        cc_id = cc.id

    update_campaign_contact_status(cc_id, status="calling", session_id=call_sid)
    save_outbound_call(lead_id=None, phone_number="+919876001122", provider_call_id=call_sid, status="queued", provider="exotel", campaign_id=campaign.id)

    with patch("backend.telephony.exotel.ExotelProvider.verify_webhook_signature", return_value=True):
        resp = client.post(
            "/api/telephony/webhook/exotel/status",
            data={"CallSid": call_sid, "Status": "no-answer", "DialCallDuration": "0"},
        )
    assert resp.status_code == 200
    assert get_outbound_call_by_sid(call_sid).call_status == "no_answer"

    with SessionLocal() as db:
        cc_after = db.get(CampaignContact, cc_id)
        assert cc_after.status == "no_answer"
        assert cc_after.call_outcome == "no_answer"


