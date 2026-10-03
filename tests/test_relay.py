"""The relay: Claude Code (Anthropic's Messages API) on a provider that speaks OpenAI's Chat Completions API."""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from clodfarm import bots, prices, relay
from clodfarm.agents import AgentManager
from clodfarm.config import load


def _chunk(delta=None, finish=None, usage=None, index=0):
    c = {"id": "chatcmpl-1", "choices": [] if delta is None and finish is None else
         [{"index": index, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        c["usage"] = usage
    return c


USAGE = {"prompt_tokens": 1200, "completion_tokens": 80, "prompt_tokens_details": {"cached_tokens": 1000}}
SIG = {"google": {"thought_signature": "c2lnbmF0dXJl" * 20}}


@pytest.fixture
def upstream():
    """A stand-in for an OpenAI-compatible provider. The last user words pick the answer: TEXT (reasoning, then text),
    TOOLS (two parallel tool calls, interleaved, one with a Gemini thought signature), THOUGHT (Gemini's <thought>
    tags, cut across chunks), BUSY (429), FULL (a context overflow). Key `good` only."""
    seen = []

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, code, body, headers=None):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            if self.path == "/v1/responses":
                return self._responses(body)
            if self.path != "/v1/chat/completions":
                return self._json(404, {"error": {"message": "no route"}})
            if self.headers.get("Authorization") != "Bearer good":
                return self._json(401, {"error": {"message": "Incorrect API key provided"}})
            last = body["messages"][-1]
            words = last["content"] if isinstance(last["content"], str) else json.dumps(last["content"])
            if "BUSY" in words:
                return self._json(429, {"error": {"message": "Rate limit reached"}}, {"retry-after": "7"})
            if "FULL" in words:
                return self._json(400, {"error": {"message": "This model's maximum context length is 128000 tokens",
                                                  "code": "context_length_exceeded"}})
            if "TOOLS" in words:
                chunks = [_chunk({"role": "assistant", "content": "Looking."}),
                          _chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "extra_content": SIG,
                                                  "function": {"name": "Bash", "arguments": '{"comm'}}]}),
                          _chunk({"tool_calls": [{"index": 1, "id": "call_b", "type": "function",
                                                  "function": {"name": "mcp__a_very_long_server_name__with_a_much_"
                                                               "longer_tool_name_than_openai_allows",
                                                               "arguments": '{"q": 1}'}}]}),
                          _chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'and": "ls"}'}}]}),
                          _chunk(finish="tool_calls"), _chunk(usage=USAGE)]
            elif "SIGNED" in words:  # Gemini 3: the message's thought signature comes after its text
                chunks = [_chunk({"content": "<thought>sure</thought>"}), _chunk({"content": "832040"}),
                          {"choices": [{"index": 0, "delta": {"extra_content": SIG}}]}, _chunk(usage=USAGE)]
            elif "THOUGHT" in words:
                chunks = [_chunk({"content": "<thou"}), _chunk({"content": "ght>planning</tho"}),
                          _chunk({"content": "ught>Done."}), _chunk(finish="stop"), _chunk(usage=USAGE)]
            else:
                chunks = [_chunk({"role": "assistant", "reasoning_content": "Let me "}),
                          _chunk({"reasoning_content": "think."}), _chunk({"content": "ok"}),
                          _chunk(finish="stop"), _chunk(usage=USAGE)]
            if not body.get("stream"):
                msg = {"role": "assistant", "content": "".join(c["choices"][0]["delta"].get("content") or ""
                                                                for c in chunks if c["choices"]),
                       "reasoning_content": "".join(c["choices"][0]["delta"].get("reasoning_content") or ""
                                                    for c in chunks if c["choices"]) or None}
                return self._json(200, {"id": "x", "choices": [{"message": msg, "finish_reason": "stop"}],
                                        "usage": USAGE})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.close_connection = True

        def _responses(self, body):
            """OpenAI's Responses API: reasoning (a summary, then the encrypted item), then text or two calls."""
            if body.get("reasoning") and "nope" in body["model"]:
                return self._json(400, {"error": {"message": "reasoning is not supported with this model"}})
            words = json.dumps(body["input"][-1])
            out = [{"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "Plan it."}],
                    "encrypted_content": "ENC1"}]
            if "TOOLS" in words:
                out += [{"type": "function_call", "id": "fc_1", "call_id": "call_a", "name": "Bash",
                         "arguments": '{"command": "ls"}'},
                        {"type": "function_call", "id": "fc_2", "call_id": "call_b", "name": "Bash",
                         "arguments": '{"command": "pwd"}'}]
            else:
                out.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]})
            usage = {"input_tokens": 1200, "input_tokens_details": {"cached_tokens": 1000}, "output_tokens": 80}
            if not body.get("stream"):
                return self._json(200, {"id": "resp_1", "output": out, "usage": usage, "status": "completed"})
            evs = [{"type": "response.created", "response": {}}]
            for item in out:
                evs.append({"type": "response.output_item.added", "item": {"type": item["type"]}})
                if item["type"] == "reasoning":
                    evs += [{"type": "response.reasoning_summary_part.added"},
                            {"type": "response.reasoning_summary_text.delta", "delta": "Plan "},
                            {"type": "response.reasoning_summary_text.delta", "delta": "it."}]
                elif item["type"] == "message":
                    evs.append({"type": "response.output_text.delta", "delta": "ok"})
                evs.append({"type": "response.output_item.done", "item": item})
            evs.append({"type": "response.completed", "response": {"usage": usage, "status": "completed"}})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for e in evs:
                self.wfile.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())
            self.close_connection = True

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1", seen
    srv.shutdown()


@pytest.fixture
def running(upstream):
    """A relay in this process, in front of the stand-in provider."""
    url, seen = upstream
    st = {"provider": "gemini", "url": url, "model": "gemini-2.5-pro", "effort": "", "max_out": 0}
    srv = relay.serve(relay.Relay(st, "good", "tok"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


def _post(base, body, token="tok"):
    req = urllib.request.Request(base + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _events(raw: bytes):
    out = []
    for frame in raw.decode().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "data" in lines:
            out.append(json.loads(lines["data"]))
    return out


def _message(events):
    """The message a stream builds, the way Anthropic's SDK puts it together."""
    msg, blocks, partial = None, {}, {}
    for e in events:
        if e["type"] == "message_start":
            msg = e["message"]
        elif e["type"] == "content_block_start":
            blocks[e["index"]] = dict(e["content_block"])
        elif e["type"] == "content_block_delta":
            d, b = e["delta"], blocks[e["index"]]
            if d["type"] == "text_delta":
                b["text"] += d["text"]
            elif d["type"] == "thinking_delta":
                b["thinking"] += d["thinking"]
            elif d["type"] == "signature_delta":
                b["signature"] = d["signature"]
            elif d["type"] == "input_json_delta":
                partial[e["index"]] = partial.get(e["index"], "") + d["partial_json"]
        elif e["type"] == "message_delta":
            msg.update(e["delta"])
            msg["usage"].update(e["usage"])
    for i, j in partial.items():
        blocks[i]["input"] = json.loads(j)
    msg["content"] = [blocks[i] for i in sorted(blocks)]
    return msg


CONVERSATION = {
    "model": "claude-opus-4", "max_tokens": 32000, "stream": False, "temperature": 1,
    "system": [{"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}},
               {"type": "text", "text": "Farm guide."}],
    "tools": [{"name": "Bash", "description": "Run a command",
               "input_schema": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                                "additionalProperties": False, "required": ["command"],
                                "properties": {"command": {"type": "string", "format": "uri"},
                                               "timeout": {"type": "number", "exclusiveMinimum": 0}}}},
              {"type": "web_search_20250305", "name": "web_search"}],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "list files"},
                                     {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                  "data": "iVBOR"}}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm", "signature": "clodfarm-relay"},
            {"type": "text", "text": "Running ls."},
            {"type": "tool_use", "id": relay.encode_id("call_a", SIG), "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_use", "id": "call_b", "name": "Bash", "input": {"command": "pwd"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": relay.encode_id("call_a", SIG), "content": "a.txt"},
            {"type": "tool_result", "tool_use_id": "call_b", "is_error": True,
             "content": [{"type": "text", "text": "no such dir"}]},
            {"type": "text", "text": "and now?"}]},
    ]}


def test_a_conversation_is_put_the_way_openai_takes_it():
    st = {"provider": "gemini", "url": "x", "model": "gemini-2.5-pro", "effort": "low", "max_out": 8192}
    body, names = relay.to_chat(CONVERSATION, st)
    m = body["messages"]
    assert body["model"] == "gemini-2.5-pro", "always the bot's own model, whatever Claude Code asks for"
    assert m[0] == {"role": "system", "content": "You are Claude Code.\nFarm guide."}
    assert m[1]["content"][0] == {"type": "text", "text": "list files"}
    assert m[1]["content"][1]["image_url"]["url"] == "data:image/png;base64,iVBOR"
    a = m[2]
    assert a["role"] == "assistant" and a["content"] == "Running ls."
    assert a["tool_calls"][0] == {"id": "call_a", "type": "function", "extra_content": SIG,
                                  "function": {"name": "Bash", "arguments": '{"command": "ls"}'}}, \
        "the thought signature goes back with its tool call"
    assert "extra_content" not in a["tool_calls"][1]
    assert m[3] == {"role": "tool", "tool_call_id": "call_a", "content": "a.txt"}, "results right after their calls"
    assert m[4] == {"role": "tool", "tool_call_id": "call_b", "content": "Error: no such dir"}
    assert m[5] == {"role": "user", "content": "and now?"}
    assert [t["function"]["name"] for t in body["tools"]] == ["Bash"], "Anthropic's own server tools are left out"
    params = body["tools"][0]["function"]["parameters"]
    assert "$schema" not in params and "additionalProperties" not in params
    assert params["properties"]["command"] == {"type": "string"}, "Gemini takes no format but enum and date-time"
    assert params["properties"]["timeout"] == {"type": "number"}
    assert body["max_tokens"] == 8192, "capped at the bot's max output"
    assert body["reasoning_effort"] == "low" and body["temperature"] == 1
    assert body["extra_body"]["google"]["thinking_config"]["include_thoughts"], "Gemini shows its thoughts"
    assert names == {"Bash": "Bash"}

    body, _ = relay.to_chat({**CONVERSATION, "stream": True}, {**st, "provider": "openai", "effort": "", "max_out": 0})
    assert body["max_completion_tokens"] == 32000 and "max_tokens" not in body and "temperature" not in body
    assert body["stream_options"] == {"include_usage": True} and "extra_body" not in body
    assert "additionalProperties" in body["tools"][0]["function"]["parameters"], "only Gemini needs the cleanup"


def test_tool_names_and_ids_survive_the_round_trip():
    long = "mcp__a_very_long_server_name__with_a_much_longer_tool_name_than_openai_allows"
    n = relay.tool_name(long)
    assert len(n) <= 64 and relay.NAME_OK.match(n) and relay.tool_name(long) == n
    assert relay.tool_name("mcp__stripe__create_product") == "mcp__stripe__create_product"
    tid = relay.encode_id("call_a", SIG)
    assert relay.NAME_OK.pattern and all(c.isalnum() or c in "_-" for c in tid)
    assert relay.decode_id(tid) == ("call_a", SIG)
    assert relay.encode_id("call_b", None) == "call_b" and relay.decode_id("call_b") == ("call_b", None)
    assert relay.decode_id(relay.encode_id("weird id!", None)) == ("weird id!", None)
    assert relay.decode_id("toolu_xnot-base64!") == ("toolu_xnot-base64!", None)


def test_gemini_thoughts_are_split_out_even_when_a_tag_is_cut():
    t = relay.Thoughts()
    out = t.feed("<thou") + t.feed("ght>plan") + t.feed("ning</tho") + t.feed("ught>Do") + t.feed("ne <b>") + t.flush()
    merged = []
    for kind, s in out:
        if merged and merged[-1][0] == kind:
            merged[-1] = (kind, merged[-1][1] + s)
        else:
            merged.append((kind, s))
    assert merged == [("thinking", "planning"), ("text", "Done <b>")]


def test_a_stream_comes_back_the_way_anthropic_streams(running):
    base, seen = running
    code, raw, headers = _post(base, {**CONVERSATION, "stream": True,
                                      "messages": [{"role": "user", "content": "say TEXT"}]})
    assert code == 200 and headers["Content-Type"] == "text/event-stream"
    ev = _events(raw)
    assert ev[0]["type"] == "message_start" and ev[-1]["type"] == "message_stop"
    msg = _message(ev)
    assert msg["content"][0] == {"type": "thinking", "thinking": "Let me think.", "signature": "clodfarm-relay"}
    assert msg["content"][1] == {"type": "text", "text": "ok"}
    assert msg["stop_reason"] == "end_turn"
    assert msg["usage"] == {"input_tokens": 200, "output_tokens": 80, "cache_read_input_tokens": 1000,
                            "cache_creation_input_tokens": 0}
    assert seen[-1]["auth"] == "Bearer good", "the provider gets its own key, never the relay's token"
    assert seen[-1]["body"]["stream"] is True


def test_tool_calls_come_back_whole_and_go_back_with_what_the_provider_needs(running):
    base, seen = running
    code, raw, _ = _post(base, {**CONVERSATION, "stream": True, "tools": CONVERSATION["tools"][:1] + [
        {"name": "mcp__a_very_long_server_name__with_a_much_longer_tool_name_than_openai_allows",
         "input_schema": {"type": "object", "properties": {"q": {"type": "number"}}}}],
        "messages": [{"role": "user", "content": "use TOOLS"}]})
    assert code == 200
    msg = _message(_events(raw))
    assert msg["stop_reason"] == "tool_use"
    assert msg["content"][0] == {"type": "text", "text": "Looking."}
    a, b = msg["content"][1], msg["content"][2]
    assert a["type"] == b["type"] == "tool_use"
    assert a["name"] == "Bash" and a["input"] == {"command": "ls"}, "interleaved arguments, put back together"
    assert b["name"].startswith("mcp__a_very_long") and b["input"] == {"q": 1}, "its long name, as Claude Code knows it"
    assert relay.decode_id(a["id"]) == ("call_a", SIG) and b["id"] == "call_b"
    # the next turn: Claude Code sends those blocks back with their results
    _post(base, {**CONVERSATION, "stream": False, "messages": [
        {"role": "user", "content": "use TOOLS"},
        {"role": "assistant", "content": msg["content"]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": a["id"], "content": "a.txt"},
                                     {"type": "tool_result", "tool_use_id": b["id"], "content": "1"}]}]})
    sent = [m for m in seen[-1]["body"]["messages"] if m["role"] != "system"]
    calls = sent[1]["tool_calls"]
    assert calls[0]["id"] == "call_a" and calls[0]["extra_content"] == SIG
    assert calls[1]["function"]["name"] == relay.tool_name(b["name"])
    assert [(x["role"], x["tool_call_id"]) for x in sent[2:]] == [("tool", "call_a"), ("tool", "call_b")]


def test_gemini_thoughts_stream_as_thinking(running):
    base, _ = running
    msg = _message(_events(_post(base, {**CONVERSATION, "stream": True,
                                        "messages": [{"role": "user", "content": "THOUGHT"}]})[1]))
    assert [(b["type"], b.get("thinking") or b.get("text")) for b in msg["content"]] == \
        [("thinking", "planning"), ("text", "Done.")]


def test_a_whole_answer_without_streaming(running):
    base, _ = running
    code, raw, _ = _post(base, {**CONVERSATION, "messages": [{"role": "user", "content": "TEXT"}]})
    got = json.loads(raw)
    assert code == 200 and got["type"] == "message" and got["role"] == "assistant"
    assert got["content"] == [{"type": "thinking", "thinking": "Let me think.", "signature": "clodfarm-relay"},
                              {"type": "text", "text": "ok"}]
    assert got["usage"]["cache_read_input_tokens"] == 1000


def test_errors_are_said_the_way_claude_code_understands(running):
    base, _ = running
    code, raw, headers = _post(base, {**CONVERSATION, "messages": [{"role": "user", "content": "BUSY"}]})
    assert code == 429 and json.loads(raw)["error"]["type"] == "rate_limit_error" and headers["retry-after"] == "7"
    code, raw, _ = _post(base, {**CONVERSATION, "messages": [{"role": "user", "content": "FULL"}]})
    err = json.loads(raw)["error"]
    assert code == 400 and err["message"].startswith("prompt is too long"), "Claude Code compacts and goes on"
    code, raw, _ = _post(base, {**CONVERSATION, "messages": [{"role": "user", "content": "hi"}]}, token="guess")
    assert code == 401, "only its bot's Claude Codes, with the relay's token"
    req = urllib.request.Request(base + "/v1/messages/count_tokens", method="POST",
                                 data=json.dumps(CONVERSATION).encode(), headers={"Authorization": "Bearer tok"})
    assert json.loads(urllib.request.urlopen(req, timeout=10).read())["input_tokens"] > 0
    assert relay.error_of(503, b"{}")[0] == 529
    assert relay.error_of(400, b'[{"error": {"message": "input token count exceeds the maximum"}}]')[1]["error"][
        "message"].startswith("prompt is too long"), "Gemini's list-shaped errors"


def test_a_bot_on_the_relay_is_checked_through_it(upstream):
    url, seen = upstream
    bot = bots.parse({"provider": "openai-compatible", "url": url + "/chat/completions", "model": "m"})
    assert bot["url"] == url and bots.relayed(bot)
    assert bots.check(bot, "good") == "ok"
    assert seen[-1]["body"]["tools"][0]["function"]["name"] == "farm_probe", "Claude Code's tools must be taken"
    with pytest.raises(ValueError, match="refused the key"):
        bots.check(bot, "bad")
    with pytest.raises(ValueError, match="can't reach"):
        bots.check({**bot, "url": "http://127.0.0.1:9/v1"}, "good")
    g = bots.parse({"provider": "gemini", "model": "gemini-2.5-pro", "effort": "high", "price_in": "1.25",
                    "price_out": 10, "daily_usd": 20})
    assert g["url"] == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert g["effort"] == "high" and g["price"] == {"in": 1.25, "out": 10.0} and g["daily_usd"] == 20
    with pytest.raises(ValueError, match="effort"):
        bots.parse({"provider": "xai", "model": "grok-4", "effort": "max"})
    with pytest.raises(ValueError, match="relay"):
        bots.parse({"provider": "openrouter", "model": "m", "effort": "low"})


def test_list_prices():
    assert prices.price("openai", "gpt-5-mini-2025-08-07") == prices.TABLE["openai"]["gpt-5-mini"], "longest prefix"
    assert prices.price("xai", "grok-4-0709") == prices.TABLE["xai"]["grok-4"]
    assert prices.price("openai", "some-new-model") is None
    assert prices.price("xai", "grok-4.7") == (2.00, 0.50, 6.00), "the newer model's own price, not grok-4's"
    assert prices.price("openai", "gpt-6.1-sol") == (2.00, 0.10, 10.00)
    assert prices.price("gemini", "gemini-3.1-pro-preview") == (2.00, 0.20, 12.00)
    assert prices.price("xai", "grok-9", {"in": 2, "out": 6}) == (2.0, 2.0, 6.0), "the bot's own price wins"
    u = {"input_tokens": 1_000_000, "cache_read_input_tokens": 2_000_000, "output_tokens": 500_000}
    assert prices.cost("gemini", "gemini-2.5-pro", u) == pytest.approx(1.25 + 2 * 0.31 + 5.0)
    assert prices.cost("openai", "unknown", u) == 0


def test_a_bot_on_the_relay_runs_through_its_own_relay_process(env, upstream, monkeypatch):
    """Its `clodfarm run` starts the relay detached, every Claude Code it starts talks to it, the provider's key stays
    with the relay, and its runs are counted at its model's list price."""
    from test_farm import calls, start_farm, stop_farm, wait_for
    from conftest import cli
    url, seen = upstream
    mgr = AgentManager(load())
    mgr.stopping.set()
    bot = bots.parse({"provider": "openai-compatible", "url": url, "model": "m", "price_in": 0, "price_out": 1000})
    agent = mgr.create("gpt", bot=bot, key="good")
    e = mgr.env_for(agent)
    assert e["FARM_BOT_DIALECT"] == "chat" and e["FARM_BOT_UPSTREAM"] == url and "good" not in json.dumps(e), \
        "the key stays in its config dir: the relay reads it there"
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    farm, t = start_farm()
    ws = str(env / "workspace")
    try:
        st = relay.procs.read_json(relay.state_path(ws, "gpt"))
        assert st.get("port") and st.get("token")
        base = f"http://127.0.0.1:{st['port']}"
        assert json.loads(urllib.request.urlopen(base + "/healthz", timeout=5).read())["model"] == "m"
        code, raw, _ = _post(base, {"max_tokens": 50, "messages": [{"role": "user", "content": "TEXT"}]}, st["token"])
        assert code == 200 and json.loads(raw)["content"][-1]["text"] == "ok"
        assert seen[-1]["auth"] == "Bearer good", "the relay read the bot's key from its config dir"
        tid = json.loads(cli("spawn", "for gpt", "--prompt", "COMMIT hi", "--on", "gpt", "--json").stdout)["id"]
        wait_for(lambda: farm.store.get_task(tid)["status"] == "done", timeout=60)
        runs = [c for c in calls(env) if c["cmd"] == "print"]
        assert runs and all(c["base_url"] == base and c["token"] == st["token"] for c in runs)
        # fake_claude says 42 output tokens and $0.01 (as if it were Claude): the bot's price says $0.042
        assert farm.store.spent_today("bot-gpt") == pytest.approx(42 * 1000 / 1e6)
        assert relay.ensure(ws, "gpt", dict(os.environ)) == (base, st["token"]), "the same relay, found again"
    finally:
        stop_farm(farm, t)
    pid = relay.procs.read_json(relay.state_path(ws, "gpt"))["pid"]
    assert relay.procs.alive(pid), "it outlives its bot's `clodfarm run`: a new release adopts the runs that use it"
    mgr.remove("gpt")
    t0 = time.time()
    while relay.procs.alive(pid) and time.time() - t0 < 10:
        time.sleep(0.1)
    assert not relay.procs.alive(pid), "released with its bot"


def test_a_signature_after_the_text_rides_on_the_thinking_and_goes_back(running):
    """Claude Code takes a run's result from the last block of its last message: the text must stay last."""
    base, seen = running
    msg = _message(_events(_post(base, {**CONVERSATION, "stream": True,
                                        "messages": [{"role": "user", "content": "SIGNED"}]})[1]))
    assert [b["type"] for b in msg["content"]] == ["thinking", "text"] and msg["content"][1]["text"] == "832040"
    assert relay.unsign(msg["content"][0]["signature"]) == SIG
    _post(base, {**CONVERSATION, "messages": [{"role": "user", "content": "SIGNED"},
                                              {"role": "assistant", "content": msg["content"]},
                                              {"role": "user", "content": "TEXT"}]})
    back = [m for m in seen[-1]["body"]["messages"] if m["role"] == "assistant"][0]
    assert back["content"] == "832040" and back["extra_content"] == SIG
    assert relay.unsign("clodfarm-relay") is None and relay.unsign("other") is None


@pytest.fixture
def openai_relay(upstream):
    url, seen = upstream
    st = {"provider": "openai", "dialect": "responses", "url": url, "model": "gpt-6.1-sol", "effort": "high",
          "max_out": 0}
    srv = relay.serve(relay.Relay(st, "good", "tok"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


def test_openai_gets_a_responses_request():
    st = {"provider": "openai", "dialect": "responses", "url": "x", "model": "gpt-6.1-sol", "effort": "high",
          "max_out": 0}
    conv = {**CONVERSATION, "messages": CONVERSATION["messages"][:1] + [
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm", "signature": relay.sign({"openai": {
                "type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC1"}})},
            {"type": "text", "text": "Running ls."},
            {"type": "tool_use", "id": "call_a", "name": "Bash", "input": {"command": "ls"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_a", "content": "a.txt"},
                                     {"type": "text", "text": "and now?"}]}]}
    body, names = relay.to_responses(conv, st)
    assert body["instructions"] == "You are Claude Code.\nFarm guide." and body["store"] is False
    assert body["reasoning"] == {"summary": "auto", "effort": "high"}
    assert body["include"] == ["reasoning.encrypted_content"], "its reasoning comes back to the farm, encrypted"
    assert body["max_output_tokens"] == 32000 and "temperature" not in body
    items = body["input"]
    assert items[0]["role"] == "user" and items[0]["content"][0] == {"type": "input_text", "text": "list files"}
    assert items[0]["content"][1]["type"] == "input_image"
    assert items[1] == {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC1"}, \
        "the reasoning goes back before the call it led to"
    assert items[2] == {"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "Running ls."}]}
    assert items[3] == {"type": "function_call", "call_id": "call_a", "name": "Bash",
                        "arguments": '{"command": "ls"}'}
    assert items[4] == {"type": "function_call_output", "call_id": "call_a", "output": "a.txt"}
    assert items[5] == {"role": "user", "content": [{"type": "input_text", "text": "and now?"}]}
    assert [t["name"] for t in body["tools"]] == ["Bash"] and body["tools"][0]["strict"] is False


def test_openai_streams_its_reasoning_and_calls_and_gets_them_back(openai_relay):
    base, seen = openai_relay
    code, raw, _ = _post(base, {**CONVERSATION, "stream": True, "messages": [{"role": "user", "content": "TOOLS"}]})
    assert code == 200 and seen[-1]["path"] == "/v1/responses"
    msg = _message(_events(raw))
    th, a, b = msg["content"]
    assert th["type"] == "thinking" and th["thinking"] == "Plan it.", "its reasoning summary, as thinking"
    assert relay.unsign(th["signature"])["openai"]["encrypted_content"] == "ENC1"
    assert (a["name"], a["input"], b["input"]) == ("Bash", {"command": "ls"}, {"command": "pwd"})
    assert msg["stop_reason"] == "tool_use"
    assert msg["usage"] == {"input_tokens": 200, "output_tokens": 80, "cache_read_input_tokens": 1000,
                            "cache_creation_input_tokens": 0}
    _post(base, {**CONVERSATION, "messages": [
        {"role": "user", "content": "TOOLS"}, {"role": "assistant", "content": msg["content"]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": a["id"], "content": "x"},
                                     {"type": "tool_result", "tool_use_id": b["id"], "content": "y"}]}]})
    sent = seen[-1]["body"]["input"]
    assert [i.get("type") for i in sent[1:]] == ["reasoning", "function_call", "function_call",
                                                 "function_call_output", "function_call_output"]
    assert sent[1]["encrypted_content"] == "ENC1" and sent[4]["call_id"] == "call_a"


def test_openai_without_streaming_and_a_model_without_reasoning(openai_relay, upstream):
    base, _ = openai_relay
    got = json.loads(_post(base, {**CONVERSATION, "messages": [{"role": "user", "content": "hi"}]})[1])
    assert [b["type"] for b in got["content"]] == ["thinking", "text"] and got["content"][1]["text"] == "ok"
    url, seen = upstream
    bot = bots.parse({"provider": "openai", "url": url, "model": "gpt-nope"})
    assert bots.check(bot, "good") == "ok", "a model without reasoning: asked again without it"
    assert "reasoning" in seen[-2]["body"] and "reasoning" not in seen[-1]["body"]

