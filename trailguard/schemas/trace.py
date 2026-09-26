"""Evidence trace contracts (spec sections 18, 10.6).

The trace is a structured, machine-readable record of what the agent actually
did: which tools it called with which arguments, which horizons it checked,
whether freshness was verified, which rules fired and what it finally claimed.
It exists so a judge can inspect behaviour instead of trusting a console log,
and so the no-skill / with-skill comparison can be computed rather than argued.

Security note: every free-text and mapping field of the trace is a potential
leak, not just tool arguments. A result summary, an error string, a freshness
log line, a weather source name or a sampling-parameter map may legitimately
carry credential-shaped values (an API key in a header, a token inside a device
id, a bearer string in a provider URL). Records built through
:class:`ToolCallRecord`, :class:`EvidenceTrace`, :meth:`EvidenceTrace.redact_mapping`
and :func:`_redact_secret_text` therefore redact those values *at the contract
boundary*, so a trace can be printed, serialized or attached to a benchmark
report without ever holding a live secret.

Two complementary policies live here:

* ``_redact_secret_values`` -- key-based, replacing the *value* of any key whose
  name matches :data:`SECRET_KEY_PATTERNS`, recursively. Used for every mapping
  shaped field (tool arguments, ``sampling_params``).
* ``_redact_secret_text`` -- shape-based, replacing the *value* of a
  ``key: value`` / ``key=value`` pair inside a free-text field. Used for every
  string shaped field (``result_summary``, ``error``, ``freshness_checks``,
  ``weather_source``), where there is no key to match on.

Both over-redact on purpose: a legitimate ``monkey_count`` loses its value rather
than a credential gets through. Over-redaction of a demo log line is a cosmetic
cost; a leaked credential is not recoverable.
"""

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .risk import Horizon, RuleDriver, RiskLevel
from .scenario import ToolName

#: Placeholder written in place of a redacted argument value.
REDACTED = "<redacted>"

#: Case-insensitive substrings that mark an argument key as secret-shaped. The
#: match is deliberately a substring test on the *lowercased* key name so that
#: variants such as ``X-Api-Key`` or ``auth_token`` are caught too.
SECRET_KEY_PATTERNS: tuple[str, ...] = (
    "key",
    "token",
    "secret",
    "password",
    "authorization",
    "api_key",
    "credential",
)


#: ``secret-ish: value`` or ``secret-ish=value`` inside free text. The key part
#: is any run of identifier characters that *contains* one of the secret key
#: patterns; the separator tolerates the trailing quote of a JSON/YAML key; the
#: value part is one or more whitespace-separated runs of non-delimiter
#: characters, so ``Authorization: Bearer sk-...`` loses the whole credential
#: and not just the word ``Bearer``.
_SECRET_TEXT_ASSIGNMENT = re.compile(
    r"(?ix)"
    r"(?P<key>[A-Za-z0-9_.\-]*?(?:{patterns})[A-Za-z0-9_.\-]*)"
    r"(?P<sep>[ \t]*[\"']?[ \t]*(?::=|=>|[:=])[ \t]*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>[^\s\"'`,;}}\]]+(?:[ \t]+[^\s\"'`,;}}\]]+)*)"
)

_SECRET_KEY_ALTERNATION: str = "|".join(re.escape(pattern) for pattern in SECRET_KEY_PATTERNS)
_SECRET_TEXT_ASSIGNMENT = re.compile(
    _SECRET_TEXT_ASSIGNMENT.pattern.format(patterns=_SECRET_KEY_ALTERNATION)
)

#: Unquoted placeholders that carry no secret and must survive verbatim, so a
#: published ``token: null`` line is not mangled into ``token: <redacted>``.
_NON_SECRET_PLACEHOLDERS: frozenset[str] = frozenset({"none", "null", "empty", "unset", "n/a", "-"})


def _redact_secret_text(text: str | None) -> str | None:
    """Return ``text`` with credential-shaped ``key: value`` pairs masked.

    The mapping policy cannot help here: a result summary or an error message is
    free text, so there is no dictionary key to match on. This walks the text for
    the *shape* of an assignment instead.

    Responsibility: keep a secret-shaped value out of the trace no matter which
    field it was pasted into. Not responsible for: parsing the text (it is a log
    line, not a document) or deciding that a matched key is not really a
    credential -- the same over-redaction trade-off as the mapping policy: a
    legitimate ``the key: 42 m`` note loses its number, which is cosmetic.
    """

    if not text:
        return text

    def _mask(match: "re.Match[str]") -> str:
        key, sep, quote, value = match.group("key", "sep", "quote", "value")
        if not quote and value.strip().lower() in _NON_SECRET_PLACEHOLDERS:
            return match.group(0)
        # Only the *opening* quote is re-emitted: for a quoted value the closing
        # quote is still in the text right after the match, so appending one here
        # would produce ``"<redacted>""``.
        return f"{key}{sep}{quote}{REDACTED}"

    return _SECRET_TEXT_ASSIGNMENT.sub(_mask, text)


#: Decoding-parameter key names the secret-key policy deliberately does not
#: touch. ``max_tokens`` contains the ``token`` substring, so the substring scan
#: would otherwise blank out a value spec 18 requires the trace to keep. Listed by
#: name on purpose: an omission here costs a cosmetic log value, an over-broad
#: pattern here would cost a leaked credential.
_DECODING_PARAMETER_KEYS: frozenset[str] = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "max_steps",
        "seed",
        "frequency_penalty",
        "presence_penalty",
    }
)


def _redact_secret_values(mapping: Any) -> Any:
    """Return ``mapping`` with values under secret-shaped keys replaced.

    Applied recursively to nested mappings. Values whose key does not match a
    secret pattern are passed through unchanged.
    """
    if not isinstance(mapping, dict):
        return mapping
    redacted: dict[Any, Any] = {}
    for arg_key, arg_value in mapping.items():
        if isinstance(arg_key, str) and any(pattern in arg_key.lower() for pattern in SECRET_KEY_PATTERNS):
            redacted[arg_key] = REDACTED
        else:
            redacted[arg_key] = _redact_secret_values(arg_value)
    return redacted


class ToolCallRecord(BaseModel):
    """One tool invocation recorded in an evidence trace.

    Responsibility: what was called, with (redacted) arguments, when, and
    whether it succeeded. Not responsible for: executing anything -- this is a
    record, produced by the agent trace collector.
    """

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(..., description="1-based sequence number of the call within the run.")
    tool: ToolName = Field(..., description="Which registered tool was invoked.")
    arguments: dict[str, Any] = Field(default={}, description="Arguments as passed by the model, secrets redacted.")
    result_ref: str | None = Field(default=None, description="Reference/path to the full result payload.")
    result_summary: str | None = Field(default=None, description="Short human-readable summary of the result.")
    called_at: datetime = Field(..., description="ISO8601 instant the call was made.")
    status: Literal["ok", "error", "not_implemented"] = Field(
        ..., description="Outcome of the call; 'not_implemented' marks a Phase 0 stub tool."
    )
    error: str | None = Field(default=None, description="Error message when status != 'ok'.")

    @field_validator("arguments", mode="after")
    @classmethod
    def _redact_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Redact any secret-shaped argument key before the record is stored."""
        return _redact_secret_values(value)

    @field_validator("result_summary", "error", mode="after")
    @classmethod
    def _redact_text_fields(cls, value: str | None) -> str | None:
        """Mask credential-shaped ``key: value`` pairs in the free-text fields.

        A tool that echoes a provider error, or a summary that pastes a header
        it received, would otherwise park a live secret in a field nobody thought
        of as an input channel.
        """
        return _redact_secret_text(value)


class HorizonCheckRecord(BaseModel):
    """Evidence that one horizon was actually analysed (spec 10.2, 10.4).

    Responsibility: record the horizon, the weather age used with it, whether
    that age passed the freshness budget and what data was missing.
    Not responsible for: deciding the risk level or re-running the projection.
    """

    model_config = ConfigDict(extra="forbid")

    horizon: Horizon = Field(..., description="Which frozen horizon was checked.")
    weather_freshness_min: float | None = Field(default=None, description="Age of the weather snapshot used, in minutes.")
    freshness_ok: bool | None = Field(
        default=None, description="True when the snapshot was within the freshness budget; None when not checkable."
    )
    missing_data: list[str] = Field(default=[], description="Field dot-paths that were missing at this horizon.")


class EvidenceTrace(BaseModel):
    """Structured evidence trace for one agent run (spec section 18).

    Responsibility: capture tool calls, horizon coverage, freshness checks,
    weather provenance, triggered rules, risk drivers, final verdict,
    confidence, missing data, uncertainty disclosure and sampling parameters.
    Not responsible for: being a console log, a benchmark verdict, or a
    substitute for the trace collector that assembles it.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., description="Unique identifier of the run.")
    scenario_id: str | None = Field(default=None, description="Scenario the run belongs to, if any.")
    model_name: str = Field(..., description="Model that produced the run.")
    provider: str = Field(..., description="Provider that served the model.")
    skill_loaded: bool = Field(..., description="Whether SKILL.md was injected into the system prompt.")
    skill_version: str | None = Field(default=None, description="Skill version reported in SKILL.md, when present.")
    started_at: datetime = Field(..., description="ISO8601 run start instant.")
    finished_at: datetime | None = Field(default=None, description="ISO8601 run finish instant, when finished.")
    tool_calls: list[ToolCallRecord] = Field(default=[], description="Ordered record of tool invocations.")
    horizons_checked: list[Horizon] = Field(default=[], description="Horizons the agent actually analysed.")
    horizon_checks: list[HorizonCheckRecord] = Field(default=[], description="Per-horizon freshness/missing-data evidence.")
    weather_source: str | None = Field(default=None, description="Weather provider actually used.")
    weather_freshness_min: float | None = Field(default=None, description="Age of the freshest weather used, in minutes.")
    freshness_checks: list[str] = Field(default=[], description="Human-readable freshness check log entries.")
    triggered_rules: list[str] = Field(default=[], description="Rule ids reported as triggered.")
    risk_drivers: list[RuleDriver] = Field(default=[], description="Risk drivers reported by the agent.")
    final_risk_level: RiskLevel | None = Field(default=None, description="Final risk level claimed by the agent.")
    confidence: float | None = Field(default=None, description="Final confidence claimed by the agent.")
    core_risk_level: RiskLevel | None = Field(
        default=None,
        description=(
            "Highest risk level the deterministic safety core actually returned during this run, "
            "copied verbatim from the assess_risk results the model itself requested. This is what the "
            "tools produced; final_risk_level is what the agent claimed. A None here means no risk "
            "assessment was run, which is itself evidence."
        ),
    )
    core_confidence: float | None = Field(
        default=None,
        description=(
            "Confidence the deterministic safety core attached to core_risk_level. None when no "
            "assessment was run, or when the core could not compute one."
        ),
    )
    skill_sha256: str | None = Field(
        default=None,
        description=(
            "SHA-256 of the exact SKILL.md text injected into the system prompt (None in the "
            "baseline condition). Byte-level identity, so a benchmark row can be tied to the "
            "protocol version that produced it -- and so a silently edited skill cannot pass as "
            "the same one."
        ),
    )
    missing_data: list[str] = Field(default=[], description="Field dot-paths the agent reported as missing.")
    uncertainty_disclosed: bool = Field(
        default=False, description="Whether the agent explicitly disclosed uncertainty or missing data."
    )
    sampling_params: dict[str, Any] = Field(
        default={}, description="Decoding parameters used, recorded for reproducibility (temperature, max_tokens...)."
    )

    @field_validator("weather_source", mode="after")
    @classmethod
    def _redact_weather_source(cls, value: str | None) -> str | None:
        """Mask a credential-shaped ``key: value`` pair in the weather source.

        The source is normally a plain provider id (``"open-meteo"``); a provider
        configured with an inline key in its URL would not be.
        """
        return _redact_secret_text(value)

    @field_validator("freshness_checks", mode="after")
    @classmethod
    def _redact_freshness_log(cls, value: list[str]) -> list[str]:
        """Apply the text policy to every freshness log line."""
        return [redacted or entry for entry, redacted in ((line, _redact_secret_text(line)) for line in value)]

    @field_validator("sampling_params", mode="after")
    @classmethod
    def _redact_sampling_params(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Apply the mapping policy to the recorded sampling parameters.

        Sampling parameters are not credentials, but the map is a free-form
        ``dict[str, Any]`` that a caller could fill with anything, so it is
        scanned like every other mapping -- with one explicit exemption.

        ``max_tokens`` (and its decoding-parameter siblings) contain the
        ``token`` substring of :data:`SECRET_KEY_PATTERNS`, so a blanket
        substring scan would replace the very values spec 18 requires the trace
        to record for reproducibility. Those key names are therefore listed in
        :data:`_DECODING_PARAMETER_KEYS` and preserved *by name*: an explicit,
        reviewable exemption rather than a silent one. Any other key -- including
        a hypothetical ``max_tokens_backup`` or ``token`` itself -- still gets the
        ordinary substring policy and is redacted.
        """
        redacted = _redact_secret_values(value)
        for known_key in _DECODING_PARAMETER_KEYS & set(value):
            redacted[known_key] = value[known_key]
        return redacted

    @classmethod
    def redact_mapping(cls, mapping: dict) -> dict:
        """Redact secret-shaped keys in ``mapping``.

        Responsibility: expose the same redaction applied to
        :attr:`ToolCallRecord.arguments` so callers (trace collector, benchmark
        reports, demo UI) can scrub arbitrary dictionaries before logging.
        Not responsible for: deciding which keys *should* be secret -- the
        pattern list in :data:`SECRET_KEY_PATTERNS` is the whole policy.
        """
        return _redact_secret_values(mapping)
