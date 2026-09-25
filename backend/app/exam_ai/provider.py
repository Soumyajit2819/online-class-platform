from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EvaluationInput:
    exam_name: str
    question_text: str
    max_marks: float
    student_answer: str
    reference_answer: str | None = None
    rubric: str | None = None


@dataclass(frozen=True)
class EvaluationOutput:
    marks: float
    max_marks: float
    confidence: float
    reason: str
    manual_required: bool
    raw_response: dict


class EvaluationFailure(Exception):
    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class EvaluatorProvider(Protocol):
    name: str
    model: str

    async def evaluate(self, item: EvaluationInput) -> EvaluationOutput: ...
