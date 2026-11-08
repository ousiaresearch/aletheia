"""Model layer for Aletheia: local and external models behind one interface.

Each task may use a different model. External calls require explicit opt-in
and are recorded in the receipt/audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


@dataclass
class ModelCall:
    model_id: str
    provider: str
    task: str
    input_chars: int = 0
    output_chars: int = 0
    egress: bool = False
    error: Optional[str] = None


@dataclass
class ModelResult:
    text: str
    model: ModelCall
    raw: Any = None


class ModelLayerError(Exception):
    """Base error for model layer failures."""


class EgressPolicyError(ModelLayerError):
    """External inference was attempted without opt-in."""


class ModelUnavailable(ModelLayerError):
    """The requested model cannot be loaded or reached."""


class ModelLayer:
    """One interface for local and external inference."""

    def __init__(self, *, local_factory: Optional[Callable[[str], Any]] = None,
                 external_factory: Optional[Callable[[str, str], Any]] = None,
                 egress_allowed: bool = False) -> None:
        self.local_factory = local_factory
        self.external_factory = external_factory
        self.egress_allowed = egress_allowed
        self.calls: List[ModelCall] = []

    def _record(self, call: ModelCall, text: str) -> ModelResult:
        self.calls.append(call)
        return ModelResult(text=text, model=call)

    def _run_local(self, model_id: str, prompt: str, task: str) -> ModelResult:
        if self.local_factory is None:
            raise ModelUnavailable(f"no local model configured for {task}")
        model = self.local_factory(model_id)
        call = ModelCall(model_id=model_id, provider="local", task=task,
                         input_chars=len(prompt))
        try:
            text = model(prompt)
        except Exception as exc:
            call.error = str(exc)
            self.calls.append(call)
            raise ModelLayerError(f"local model failed: {exc}") from exc
        call.output_chars = len(text)
        return self._record(call, text)

    def _run_external(self, model_id: str, prompt: str, task: str, base_url: str, api_key: str) -> ModelResult:
        if not self.egress_allowed:
            raise EgressPolicyError(f"external inference requires opt-in for {task}")
        if self.external_factory is None:
            raise ModelUnavailable(f"no external factory configured for {task}")
        model = self.external_factory(model_id, base_url)
        call = ModelCall(model_id=model_id, provider="external", task=task,
                         input_chars=len(prompt), egress=True)
        try:
            text = model(prompt, api_key=api_key)
        except Exception as exc:
            call.error = str(exc)
            self.calls.append(call)
            raise ModelLayerError(f"external model failed: {exc}") from exc
        call.output_chars = len(text)
        return self._record(call, text)

    def run(self, model_id: str, prompt: str, task: str, *,
            base_url: str = "", api_key: str = "", force_local: bool = False) -> ModelResult:
        """Run a prompt through the appropriate model.

        External calls require `egress_allowed=True` unless `force_local=True`.
        Returns the result and records the call for audit.
        """
        if force_local or not base_url or not api_key:
            return self._run_local(model_id, prompt, task)
        return self._run_external(model_id, prompt, task, base_url, api_key)
