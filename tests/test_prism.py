from services.extraction.prism import PrismConfig, build_trace


def test_build_trace_maps_openai_usage():
    config = PrismConfig("key", "project-id", "https://example.test")
    trace = build_trace(
        config,
        model="gpt-test",
        input_messages=[{"role": "user", "content": "input"}],
        output_message="output",
        latency_ms=12,
        usage={"input_tokens": 3, "output_tokens": 4},
        session_id="message-1",
        agent_id="extractor",
        agent_name="Extractor",
    )

    assert trace["project_id"] == "project-id"
    assert trace["token_count_input"] == 3
    assert trace["token_count_output"] == 4
    assert trace["session_id"] == "message-1"
