"""Internal source-cache markers are never Chat Completions message fields."""

from code_agent.context.prompt import provider_messages
from code_agent.models.events import ProviderPayload
from code_agent.providers.openai_compat import OpenAICompatProvider


def test_source_snapshot_metadata_stays_local_but_real_protocol_survives():
    payload = ProviderPayload("fixture protocol")
    original = [
        {"role": "user", "content": "untrusted source data", "_context_source": "auto",
         "_source_receipt_snapshot": True, "_native_source_snapshot": True, "_internal_cache": object()},
        {"role": "assistant", "content": "working", "_provider_payload": payload},
    ]
    messages = provider_messages(original)
    assert messages[0] == {"role": "user", "content": "untrusted source data"}
    assert messages[1]["_provider_payload"] is payload
    body = OpenAICompatProvider(api_key="test", model="test")._body(messages, [], stream=False)
    assert body["messages"][1]["reasoning_content"] == "fixture protocol"
    assert all(not any(key.startswith("_") for key in message) for message in body["messages"])
    assert original[0]["_source_receipt_snapshot"] is True


def test_restored_json_payload_cannot_be_mistaken_for_live_private_protocol():
    assert provider_messages([{"role": "assistant", "content": "saved answer",
        "_provider_payload": {"reasoning_content": "serialized placeholder"}}]) == [
            {"role": "assistant", "content": "saved answer"}]
