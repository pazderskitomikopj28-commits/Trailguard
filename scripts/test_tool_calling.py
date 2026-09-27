#!/usr/bin/env python3
"""Prove that locally served Qwen3-VL produces REAL structured tool calls.

Phase 4 gate 6-10 of the frozen spec: before TrailGuard is allowed anywhere near a
local model, the *server* must be shown to return native structured `tool_calls`.

Three sequential gates, and a fourth added when an agent run asked for it:

  A  tool_choice="required"  -> the model MUST emit a tool call, and the endpoint must
                                return it as choices[0].message.tool_calls
  B  tool_choice="auto"      -> given a task it cannot answer without a tool, the model
                                chooses to call one, and again the endpoint returns it as
                                message.tool_calls
  C  tool-result continuation-> take B's assistant message verbatim, append the tool
                                result keyed by tool_call_id, and require a second
                                completion that continues from it
  D  parallel                -> ask for two calls at once and require that BOTH come back
                                as structured tool_calls (added 2026-09-27, after the S04
                                compound run emitted a batch the Harness could not use)

WHY RAW HTTP AND NOT THE openai SDK
-----------------------------------
This script speaks the wire protocol directly. A Python SDK can coerce a null into an
empty list, rebuild a nested object, or fail to surface a field it has no model for,
and any of those would let this probe report a capability the server does not have.
Printing the decoded JSON body is the strongest available evidence: what you see in
`raw_message` is what the endpoint actually sent. The SDK is still exercised later, by
trailguard.providers.local.LocalModelProvider, where it belongs.

WHAT THIS SCRIPT REFUSES TO DO
------------------------------
* It never parses a tool call out of `message.content`. It *inspects* content to detect
  the failure mode "the model is only imitating tool calling" -- a body whose content
  contains the tool-call envelope while `tool_calls` is null is a FAILURE, and no
  client-side rescue is attempted.
* It never falls back to another parser, endpoint, model or "best effort" mode.
* It never prints a credential, a reasoning blob or a chain of thought.
* It never reword a failed gate into a pass.

Run from the repository root, so the report lands in runs/phase4/ beside the agent
smoke traces (both git-ignored). Exit code is 0 only when all four gates pass.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

# Assemble the envelope from fragments so a grep for the tag finds the model's
# template and this probe's FAIL-detector, never a parser living in this file.
CALL_OPEN = "<" + "tool" + "_call>"
CALL_CLOSE = "</" + "tool" + "_call>"

DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
DEFAULT_MODEL = "qwen3-vl-30b-a3b"
PLACEHOLDER_KEY = "EMPTY"

# The one toy tool. Its body is only a schema: this probe asks for a call, it does
# not execute anything, and no network is involved on either side.
TOY_TOOL = {
    "type": "function",
    "function": {
        "name": "get_test_temperature",
        "description": (
            "Return the current temperature in Celsius for one city. "
            "This is a probe tool: it exists to test whether the server can express "
            "a tool call structurally."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name, e.g. Reykjavik.",
                }
            },
            "required": ["city"],
        },
    },
}


# The parameter names the toy tool declares REQUIRED. Checked against every
# parsed arguments object, so "arguments parses as JSON" can never stand in for
# "the call actually carries the argument the schema asked for".
TOY_REQUIRED_PARAMS: tuple[str, ...] = tuple(
    TOY_TOOL["function"]["parameters"]["required"]
)

class ProbeError(RuntimeError):
    """A gate could not be evaluated at all. Reported as FAILURE, never as PASS."""


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
def _post(base_url: str, path: str, payload: dict, timeout_s: int) -> tuple[int, object]:
    """POST JSON and return (status, decoded_body).

    Errors are raised, not swallowed: a transport failure must be visible as a
    FAILURE, and a gate that could not run must never be counted as a pass.
    """
    url = base_url.rstrip("/") + path
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            # A placeholder, because this vLLM deployment authenticates nothing.
            # It is never printed.
            "Authorization": f"Bearer {PLACEHOLDER_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw) if raw.strip() else {"_raw": raw[:2000]}
        except json.JSONDecodeError:
            return exc.code, {"_raw": raw[:2000]}
    except urllib.error.URLError as exc:
        raise ProbeError(f"cannot reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ProbeError(f"no response from {url} within {timeout_s}s") from exc


def _get(base_url: str, path: str, timeout_s: int) -> tuple[int, object]:
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, {"_raw": raw[:2000]}
    except urllib.error.URLError as exc:
        raise ProbeError(f"cannot reach {url}: {exc.reason}") from exc


# --------------------------------------------------------------------------- #
# inspection -- everything here is READ-ONLY judgment on the wire response
# --------------------------------------------------------------------------- #
def inspect_message(choice: dict, declared_params: tuple[str, ...] = ()) -> dict:
    """Describe one choice's message as raw observable facts.

    No field is interpreted. `tool_calls` is reported as whatever the server sent --
    its JSON type, its length, and each call's id/name/arguments-string verbatim --
    so the reader can see the structure rather than trust a verdict.

    `declared_params` are the parameter names the advertised tool schema declares
    required. They are checked *after* the JSON parse, because "arguments parses as
    a JSON object" is a weaker claim than "the call carries the arguments the
    schema asked for": `{}` parses cleanly and is still not a call to
    get_test_temperature(city).
    """
    msg = choice.get("message") or {}
    if not isinstance(msg, dict):
        return {"_error": "message is not an object", "_message_type": type(msg).__name__}

    content = msg.get("content")
    tool_calls = msg.get("tool_calls")

    observed = {
        "content_is_none": content is None,
        "content_is_string": isinstance(content, str),
        "content_len": len(content) if isinstance(content, str) else None,
        # The model's VISIBLE output, bounded. This is not a reasoning blob: it is
        # the answer a hiker would read, and without it "the continuation worked"
        # is an unverifiable claim. reasoning_content is never touched.
        "content_excerpt": (content[:400] if isinstance(content, str) else None),
        # The imitation detector: a body that spells the envelope in prose while
        # sending no structured tool_calls is the exact failure this gate exists to
        # catch. Reported, never parsed.
        "content_contains_tool_call_envelope": (
            isinstance(content, str) and CALL_OPEN in content
        ),
        "tool_calls_is_none": tool_calls is None,
        "tool_calls_type": type(tool_calls).__name__,
        "tool_calls_count": len(tool_calls) if isinstance(tool_calls, list) else None,
        "tool_calls": [],
        "finish_reason": choice.get("finish_reason"),
        # Presence only. A reasoning blob is never printed.
        "reasoning_content_present": bool(msg.get("reasoning_content")),
    }

    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                observed["tool_calls"].append({"_error": "not an object"})
                continue
            fn = tc.get("function") or {}
            args = fn.get("arguments") if isinstance(fn, dict) else None
            record = {
                "id_type": type(tc.get("id")).__name__,
                "id": tc.get("id"),
                "id_is_nonempty_string": isinstance(tc.get("id"), str) and bool(tc.get("id")),
                "type": tc.get("type"),
                "function_name": fn.get("name") if isinstance(fn, dict) else None,
                "function_arguments_type": type(args).__name__,
                "function_arguments": args,
                # Arguments must be a JSON *object* on the wire, and must parse.
                "arguments_parse_as_json": None,
                "arguments_is_json_object": None,
                "arguments_parse_error": None,
                "arguments_decoded": None,
                "declared_params_required": list(declared_params),
                "declared_params_missing": None,
                "declared_params_empty": None,
            }
            observed["tool_calls"].append(record)
            probe = observed["tool_calls"][-1]
            if isinstance(args, str):
                try:
                    decoded = json.loads(args)
                    probe["arguments_parse_as_json"] = True
                    probe["arguments_is_json_object"] = isinstance(decoded, dict)
                    if isinstance(decoded, dict):
                        probe["arguments_decoded"] = decoded
                        probe["arguments_keys"] = sorted(decoded.keys())
                        missing = [p for p in declared_params if p not in decoded]
                        empty = [
                            p for p in declared_params
                            if p in decoded and decoded[p] in (None, "", [], {})
                        ]
                        probe["declared_params_missing"] = missing
                        probe["declared_params_empty"] = empty
                except json.JSONDecodeError as exc:
                    probe["arguments_parse_as_json"] = False
                    probe["arguments_parse_error"] = f"{type(exc).__name__}: {exc}"

    return observed


def _require_structured_tool_calls(observed: dict, gate: str) -> list[str]:
    """Return the list of reasons this gate's response is NOT structured tool calling.

    Empty list means the gate's structural criterion is met. This is the only place a
    gate is judged, and every branch is a negative assertion -- there is no path to a
    PASS other than passing all of them.
    """
    reasons: list[str] = []

    if observed.get("_error"):
        reasons.append(f"{gate}: {observed['_error']}")
        return reasons

    if observed["tool_calls_is_none"]:
        if observed["content_contains_tool_call_envelope"]:
            reasons.append(
                f"{gate}: the model emitted the tool-call envelope AS TEXT and the "
                f"endpoint returned tool_calls=null. The server is not connected to the "
                f"parser. This is the blocking failure -- a client-side parser would "
                f"only hide it."
            )
        else:
            reasons.append(
                f"{gate}: tool_calls is null (finish_reason="
                f"{observed.get('finish_reason')!r}). The model produced no tool call."
            )
        return reasons

    if observed["tool_calls_type"] != "list":
        reasons.append(
            f"{gate}: tool_calls has JSON type {observed['tool_calls_type']!r}, "
            f"not a list."
        )
        return reasons

    if not observed["tool_calls_count"]:
        reasons.append(f"{gate}: tool_calls is an empty list, which is not a tool call.")
        return reasons

    for i, tc in enumerate(observed["tool_calls"]):
        label = f"{gate}: tool_calls[{i}]"
        if tc.get("_error"):
            reasons.append(f"{label}: {tc['_error']}")
            continue
        if not tc.get("id_is_nonempty_string"):
            reasons.append(f"{label}: id is {tc.get('id')!r}, not a non-empty string.")
        if not tc.get("function_name"):
            reasons.append(f"{label}: function.name is {tc.get('function_name')!r}.")
        if tc.get("arguments_parse_as_json") is not True:
            reasons.append(
                f"{label}: function.arguments does not parse as JSON "
                f"({tc.get('arguments_parse_error')!r})."
            )
        elif tc.get("arguments_is_json_object") is not True:
            reasons.append(f"{label}: function.arguments parses but is not a JSON object.")
        else:
            # Parsing cleanly is not the same as carrying the arguments the
            # advertised schema declared required. `{}` parses every time.
            missing = tc.get("declared_params_missing") or []
            empty = tc.get("declared_params_empty") or []
            if missing:
                reasons.append(
                    f"{label}: function.arguments omits the declared parameter(s) "
                    f"{missing}; it carried {tc.get('arguments_decoded')!r}."
                )
            if empty:
                reasons.append(
                    f"{label}: function.arguments declares {empty} with no value; "
                    f"it carried {tc.get('arguments_decoded')!r}."
                )

    return reasons


# --------------------------------------------------------------------------- #
# gates
# --------------------------------------------------------------------------- #
def gate_preflight(base_url: str, expected_model: str, timeout_s: int) -> dict:
    """Confirm the endpoint is up AND names exactly the model we mean to probe.

    A silently-picked model is how a probe ends up "passing" against the wrong
    deployment. If /v1/models does not agree, nothing else runs.
    """
    status, body = _get(base_url, "/models", timeout_s)
    if status != 200 or not isinstance(body, dict):
        raise ProbeError(f"/models returned HTTP {status}: {str(body)[:400]}")

    data = body.get("data")
    if not isinstance(data, list) or not data:
        raise ProbeError(f"/models returned no entries: {str(body)[:400]}")

    ids = [m.get("id") for m in data if isinstance(m, dict)]
    if ids != [expected_model]:
        raise ProbeError(
            f"/v1/models reports {ids!r}; this probe is for {expected_model!r} only. "
            f"Refusing to run against a different deployment."
        )
    return {
        "status": status,
        "model_ids_seen": ids,
        "max_model_len": data[0].get("max_model_len"),
    }


def run_gate(base_url: str, model: str, payload: dict, timeout_s: int,
             declared_params: tuple[str, ...] = ()) -> dict:
    """Issue one chat-completion request and describe it observably."""
    payload = dict(payload)
    payload["model"] = model
    status, body = _post(base_url, "/chat/completions", payload, timeout_s)

    if status != 200 or not isinstance(body, dict):
        return {
            "_transport_ok": False,
            "http_status": status,
            "error_body": str(body)[:1500],
            "_reasons": [f"HTTP {status} from the endpoint: {str(body)[:300]}"],
        }

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return {
            "_transport_ok": True,
            "http_status": status,
            "error_body": "response has no choices",
            "_reasons": ["the response carries no choices"],
        }

    observed = inspect_message(choices[0], declared_params=declared_params)
    observed["_transport_ok"] = True
    observed["http_status"] = status
    observed["usage"] = body.get("usage")
    # Finish reason of the whole response, kept next to the per-choice one.
    observed["model_in_response"] = body.get("model")
    return observed


def _completion(base_url: str, model: str, messages, tool_choice, tools, timeout_s: int,
                max_tokens: int, temperature: float,
                declared_params: tuple[str, ...] = ()) -> dict:
    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    return run_gate(base_url, model, payload, timeout_s, declared_params)


def gate_a_required(base_url, model, tools, timeout_s, max_tokens, temperature) -> dict:
    """tool_choice=required with a toy tool: the model has no choice but to call it."""
    observed = _completion(
        base_url, model,
        messages=[{
            "role": "user",
            "content": "Use the available function to get the temperature in Reykjavik. "
                       "Do not answer from your own knowledge.",
        }],
        tool_choice="required",
        tools=tools,
        timeout_s=timeout_s, max_tokens=max_tokens, temperature=temperature,
        declared_params=TOY_REQUIRED_PARAMS,
    )
    observed["gate"] = "A_required"
    observed["requested_tool"] = TOY_TOOL["function"]["name"]
    observed["_reasons"] = _require_structured_tool_calls(observed, "A_required")
    observed["_requested_tool_was_called"] = any(
        tc.get("function_name") == TOY_TOOL["function"]["name"]
        for tc in observed.get("tool_calls", [])
    )
    if observed["_requested_tool_was_called"] is False:
        observed["_reasons"].append(
            "A_required: the model called a different function than the one offered."
        )
    return observed


def gate_d_parallel(base_url, model, tools, timeout_s, max_tokens, temperature) -> dict:
    """More than one tool call in a single message, expressed structurally.

    Gate D exists because an agent run raised the question. In the S04 compound
    run, the model batched its calls -- five in one turn, four in the next -- and
    the harness executed every one of them from ``message.tool_calls``. Then, on the
    turn whose batch the output budget could not finish, the whole batch came back
    as content text. Nothing in gates A-C distinguishes "one call arrives
    structured" from "a batch does", and the difference is what decided that run.

    The parser is all-or-nothing across a message: it loops over every envelope it
    finds, and one envelope that does not parse discards the lot into ``content``.
    So the criterion here is not merely "some call survived" -- it is that every
    envelope in the batch round-trips, which is the only way a harness receives the
    batch the model actually emitted.
    """
    observed = _completion(
        base_url, model,
        messages=[{
            "role": "user",
            "content": "Use the available function to get the temperature in "
                       "Reykjavik AND the temperature in Nairobi, in one response. "
                       "Two calls, one for each city. Do not answer from your own "
                       "knowledge.",
        }],
        tool_choice="required",
        tools=tools,
        timeout_s=timeout_s, max_tokens=max_tokens, temperature=temperature,
        declared_params=TOY_REQUIRED_PARAMS,
    )
    observed["gate"] = "D_parallel"
    reasons = _require_structured_tool_calls(observed, "D_parallel")

    count = observed.get("tool_calls_count")
    observed["cities_requested"] = 2
    observed["calls_received"] = count if isinstance(count, int) else None
    if not isinstance(count, int) or count < 2:
        reasons.append(
            f"D_parallel: the endpoint returned {count} tool call(s) for a request "
            "that asked for two; one structured call does not prove a batch does."
        )
    else:
        # Every call must carry its own city. Two calls that repeat one city are a
        # batch that was assembled, not a batch that was understood.
        cities = sorted(
            str(tc.get("arguments_decoded", {}).get("city", ""))
            for tc in observed.get("tool_calls", [])
            if isinstance(tc.get("arguments_decoded"), dict)
        )
        observed["cities_in_arguments"] = cities
        if len(set(cities)) != 2:
            reasons.append(
                f"D_parallel: the batch carries {cities!r}; two calls must name two "
                "different cities for the batch to be a real batch."
            )
    if observed.get("finish_reason") == "length":
        reasons.append(
            "D_parallel: finish_reason is 'length', so the batch was cut off by the "
            f"output budget ({max_tokens} tokens) while it was still being written. "
            "A budget that stops a batch mid-envelope is the exact condition under "
            "which the parser discards the whole batch into content."
        )
    observed["_reasons"] = reasons
    return observed


def gate_b_auto(base_url, model, tools, timeout_s, max_tokens, temperature) -> dict:
    """tool_choice=auto with a task the model cannot answer without the tool."""
    observed = _completion(
        base_url, model,
        messages=[{
            "role": "user",
            "content": "What is the current temperature in Reykjavik? Use the "
                       "available function rather than answering from memory.",
        }],
        tool_choice="auto",
        tools=tools,
        timeout_s=timeout_s, max_tokens=max_tokens, temperature=temperature,
        declared_params=TOY_REQUIRED_PARAMS,
    )
    observed["gate"] = "B_auto"
    observed["_reasons"] = _require_structured_tool_calls(observed, "B_auto")
    return observed


def gate_c_continuation(base_url, model, tools, gate_b, timeout_s, max_tokens,
                        temperature) -> dict:
    """Replay B's assistant turn and hand back the tool result B's own call asked for.

    The strictest test of the three: the model must consume its own prior tool_calls
    and a tool-role message keyed by the call id, and keep going.
    """
    if gate_b.get("_reasons"):
        return {
            "gate": "C_continuation",
            "_skipped": "gate B produced no structured tool_calls to continue from",
            "_reasons": ["C_continuation not run: gate B has no tool call to continue"],
        }

    call = gate_b["tool_calls"][0]
    call_id = call.get("id")
    fn = TOY_TOOL["function"]["name"]

    # The assistant turn is carried over verbatim, including its tool_calls, because
    # a continuation that drops them is not testing continuation.
    assistant_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {
                "name": call.get("function_name"),
                "arguments": call.get("function_arguments"),
            },
        }],
    }
    tool_message = {
        "role": "tool",
        "tool_call_id": call_id,
        # The result of the tool B asked for. Still no execution and no network.
        "content": json.dumps({"city": "Reykjavik", "temperature_c": 4.5,
                               "observed_at": "probe-synthetic"}),
    }

    observed = _completion(
        base_url, model,
        messages=[
            {"role": "user", "content": "What is the current temperature in Reykjavik? "
                                        "Use the available function rather than "
                                        "answering from memory."},
            assistant_message,
            tool_message,
        ],
        tool_choice="auto",
        tools=tools,
        timeout_s=timeout_s, max_tokens=max_tokens, temperature=temperature,
        declared_params=TOY_REQUIRED_PARAMS,
    )
    observed["gate"] = "C_continuation"
    observed["contributed"] = {
        "tool_call_id_forwarded_verbatim": call_id,
        "tool_message_role": "tool",
        "assistant_carried_tool_calls": 1,
        "fake_result": "temperature_c=4.5",
    }
    reasons: list[str] = []
    if observed.get("_transport_ok") is False:
        reasons = list(observed.get("_reasons", []))
    elif observed.get("_reasons"):
        # It emitted another tool call instead of answering. That is legal behaviour,
        # not a failure of the transport -- but this gate requires a FINAL answer.
        reasons = [
            "C_continuation: the model produced another tool call instead of a final "
            "answer. The continuation round-trip succeeded, but no answer was formed. "
            f"(observed: {observed['tool_calls_count']} further tool call(s))"
        ]
    elif observed["content_is_none"] or not (observed.get("content_len") or 0):
        reasons = ["C_continuation: no plain-text final answer in the response."]
    elif observed["content_contains_tool_call_envelope"]:
        reasons.append(
            "C_continuation: the final answer still spells the tool-call envelope in "
            "text, so the model did not leave tool-calling format."
        )
    observed["_reasons"] = reasons
    observed["_final_answer"] = (observed.get("content_len") is not None
                                 and not observed["content_contains_tool_call_envelope"])

    # An OBSERVATION, not a gate criterion: did the answer actually use the tool
    # result, or did it answer from its own knowledge? Both are legal model
    # behaviour and neither fails this gate -- the gate is about the transport.
    # It is recorded because "the continuation worked" is only interesting if the
    # continuation *carried* something.
    excerpt = observed.get("content_excerpt") or ""
    observed["answer_uses_the_tool_result_value"] = "4.5" in excerpt
    observed["answer_names_the_asked_place"] = "reykjav" in excerpt.lower()
    return observed


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def print_gate(observed: dict, writer=sys.stdout.write) -> None:
    name = observed.get("gate", "?")
    writer(f"\n{'=' * 74}\nGATE {name}\n{'=' * 74}\n")

    if observed.get("_skipped"):
        writer(f"  SKIPPED: {observed['_skipped']}\n")

    if observed.get("_transport_ok") is False:
        writer(f"  HTTP status : {observed.get('http_status')}\n")
        writer(f"  error body  : {observed.get('error_body')}\n")
        writer(f"  VERDICT     : FAILURE\n")
        return

    writer(f"  message.tool_calls is None      : {observed.get('tool_calls_is_none')}\n")
    writer(f"  message.tool_calls JSON type    : {observed.get('tool_calls_type')}\n")
    writer(f"  message.tool_calls count        : {observed.get('tool_calls_count')}\n")
    writer(f"  content is None                 : {observed.get('content_is_none')}\n")
    if observed.get("content_len") is not None:
        writer(f"  content length (chars)          : {observed.get('content_len')}\n")
    if observed.get("content_excerpt"):
        writer(f"  content excerpt                 : "
               f"{observed['content_excerpt']!r}\n")
    writer(f"  content spells the envelope      : "
           f"{observed.get('content_contains_tool_call_envelope')}\n")
    writer(f"  reasoning_content present       : "
           f"{observed.get('reasoning_content_present')}\n")
    writer(f"  finish_reason                   : {observed.get('finish_reason')}\n")
    usage = observed.get("usage")
    if usage:
        writer(f"  usage                           : {json.dumps(usage, sort_keys=True)}\n")

    for i, tc in enumerate(observed.get("tool_calls", [])):
        writer(f"  tool_calls[{i}]:\n")
        writer(f"    id            = {tc.get('id')!r}\n")
        writer(f"    type          = {tc.get('type')!r}\n")
        writer(f"    function.name = {tc.get('function_name')!r}\n")
        args = tc.get("function_arguments")
        writer(f"    function.arguments (type {tc.get('function_arguments_type')!r}) "
               f"= {args!r}\n")
        writer(f"    arguments parse as JSON      = {tc.get('arguments_parse_as_json')}\n")
        writer(f"    arguments are a JSON object  = {tc.get('arguments_is_json_object')}\n")
        if tc.get("arguments_decoded") is not None:
            writer(f"    arguments decoded            = "
                   f"{json.dumps(tc['arguments_decoded'], sort_keys=True)}\n")
        if tc.get("declared_params_missing"):
            writer(f"    DECLARED PARAMS MISSING      = "
                   f"{tc['declared_params_missing']}\n")
        if tc.get("declared_params_empty"):
            writer(f"    DECLARED PARAMS EMPTY        = "
                   f"{tc['declared_params_empty']}\n")
        if tc.get("arguments_parse_error"):
            writer(f"    arguments parse error        = {tc['arguments_parse_error']}\n")
        if tc.get("arguments_keys"):
            writer(f"    arguments keys               = {tc['arguments_keys']}\n")

    reasons = observed.get("_reasons") or []
    if reasons:
        writer(f"  VERDICT     : FAILURE\n")
        for r in reasons:
            writer(f"    - {r}\n")
    else:
        writer(f"  VERDICT     : PASS\n")
    if observed.get("answer_uses_the_tool_result_value") is not None:
        writer(f"  answer quotes the tool's value 4.5 : "
               f"{observed['answer_uses_the_tool_result_value']}\n")
        writer(f"  answer names the asked place      : "
               f"{observed['answer_names_the_asked_place']}\n")
    writer("\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Prove structured tool calling from the locally served Qwen3-VL."
    )
    ap.add_argument("--base-url", default=os.environ.get(
        "TRAILGUARD_LOCAL_BASE_URL", DEFAULT_BASE_URL))
    ap.add_argument("--model", default=os.environ.get(
        "TRAILGUARD_LOCAL_MODEL", DEFAULT_MODEL))
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--max-tokens", type=int, default=512)
    # Gate D asks for a *batch*, and a batch needs room to be written in. The
    # default is deliberately not shared with the other gates: judging a
    # two-envelope batch on a budget built for one call would measure the budget,
    # not the endpoint. Raise or lower it to see the gate follow the budget --
    # which is the finding, not a bug in the gate.
    ap.add_argument("--parallel-max-tokens", type=int, default=2048)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--report-dir", default="runs/phase4")
    args = ap.parse_args(argv)

    out = sys.stdout.write
    started = dt.datetime.now(dt.UTC)

    out(f"TrailGuard Phase 4 -- structured tool-calling probe\n")
    out(f"  base url : {args.base_url}\n")
    out(f"  model    : {args.model}\n")
    out(f"  parser   : hermes (server-side; this script parses nothing)\n")
    out(f"  started  : {started.isoformat()}\n")

    # ---- preflight: the endpoint must name exactly this model ----------------
    try:
        preflight = gate_preflight(args.base_url, args.model, args.timeout)
    except ProbeError as exc:
        out(f"\nPREFLIGHT FAILURE: {exc}\n")
        out("No gate was attempted. Exiting non-zero.\n")
        return 2

    out(f"\nPreflight: /v1/models -> HTTP {preflight['status']}, "
        f"models {preflight['model_ids_seen']}, "
        f"max_model_len={preflight['max_model_len']}\n")

    # ---- the three gates, strictly in order --------------------------------
    gates: dict[str, dict] = {}

    out("\nGate A: tool_choice=\"required\" with a toy function.")
    gates["A_required"] = gate_a_required(
        args.base_url, args.model, [TOY_TOOL], args.timeout, args.max_tokens,
        args.temperature)

    if gates["A_required"]["_reasons"]:
        out("  Gate A failed. B and C still run -- the report needs the full picture --\n"
            "  but A failing means the parser is not returning structured calls at all.\n")

    out("\nGate B: tool_choice=\"auto\", a task that needs the tool.")
    gates["B_auto"] = gate_b_auto(
        args.base_url, args.model, [TOY_TOOL], args.timeout, args.max_tokens,
        args.temperature)

    out("\nGate C: tool-result continuation from B's own call.")
    gates["C_continuation"] = gate_c_continuation(
        args.base_url, args.model, [TOY_TOOL], gates["B_auto"], args.timeout,
        args.max_tokens, args.temperature)

    out("\nGate D: several tool calls in one message, structurally (added after the "
        "S04 compound run batched its calls).")
    gates["D_parallel"] = gate_d_parallel(
        args.base_url, args.model, [TOY_TOOL], args.timeout,
        args.parallel_max_tokens, args.temperature)

    # Every gate is printed in full -- the raw wire fields, not a summary of
    # them. A verdict line must never be the only thing a reader can see.
    for key in ("A_required", "B_auto", "C_continuation", "D_parallel"):
        print_gate(gates[key], out)

    # ---- the report ---------------------------------------------------------
    passed = {k: not g.get("_reasons") for k, g in gates.items()}
    elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds()

    out("\n" + "=" * 74 + "\nSUMMARY\n" + "=" * 74 + "\n")
    out(f"Server health        : preflight HTTP 200, models {preflight['model_ids_seen']}\n")
    out(f"Model                : {args.model}\n")
    out(f"Parser               : hermes (server-side)\n")
    out(f"Required tool call   : {'PASS' if passed['A_required'] else 'FAIL'}\n")
    out(f"Auto tool call       : {'PASS' if passed['B_auto'] else 'FAIL'}\n")
    out(f"Tool-result continuation : {'PASS' if passed['C_continuation'] else 'FAIL'}\n")
    out(f"Parallel tool calls  : {'PASS' if passed['D_parallel'] else 'FAIL'} "
        f"({gates['D_parallel'].get('calls_received')} call(s) received of "
        f"{gates['D_parallel'].get('cities_requested')} asked, at a "
        f"{args.parallel_max_tokens}-token budget)\n")

    a = gates["A_required"]
    b = gates["B_auto"]
    structured = bool(
        (not a.get("tool_calls_is_none")) and (not b.get("tool_calls_is_none"))
    )
    args_valid = all(
        tc.get("arguments_parse_as_json") is True
        for g in gates.values()
        for tc in g.get("tool_calls", [])
    ) and bool(structured)
    out(f"Structured message.tool_calls : {str(structured).lower()}\n")
    out(f"Arguments valid JSON          : {str(args_valid).lower()}\n")
    out(f"Wall clock                    : {elapsed:.1f}s\n")

    all_pass = all(passed.values())
    out(f"\nOVERALL: {'PASS -- the server returns native structured tool calls' if all_pass else 'FAIL'}\n")

    # ---- machine-readable evidence on disk ----------------------------------
    report = {
        "probe": "phase4_structured_tool_calling",
        "started_utc": started.isoformat(),
        "wall_clock_s": round(elapsed, 2),
        "base_url": args.base_url,
        "model": args.model,
        "parser": "hermes",
        "parser_is_server_side": True,
        "client_side_tool_parsing": False,
        "preflight": preflight,
        "sampling": {
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
            "parallel_max_tokens": args.parallel_max_tokens,
            "timeout_s": args.timeout,
        },
        "toy_tool": TOY_TOOL["function"]["name"],
        "gates": gates,
        "gate_verdicts": {k: ("PASS" if v else "FAIL") for k, v in passed.items()},
        "structured_message_tool_calls": structured,
        "arguments_valid_json": args_valid,
        "overall": "PASS" if all_pass else "FAIL",
    }
    report_dir = pathlib.Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "tool_calling_probe.json"
    try:
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        # A gate may pass on the wire and the evidence still not survive the run.
        # That is a failure of the phase's own record, not a pass.
        out(f"\nREPORT WRITE FAILED: {report_path}: {type(exc).__name__}: {exc}\n")
        out("The gates ran, but their evidence is not on disk. Exiting non-zero.\n")
        return 4

    written = report_path.read_text(encoding="utf-8")
    if json.loads(written).get("overall") != report["overall"]:
        out(f"\nREPORT VERIFICATION FAILED: {report_path} does not round-trip.\n")
        return 4
    out(f"Report written and read back: {report_path} ({len(written)} bytes)\n")

    return 0 if all_pass else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
