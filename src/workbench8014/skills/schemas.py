"""Skills 之间共享的稳定数据契约。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class SkillResult(BaseModel):
    success: bool
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None


class SymbolicVerificationInput(BaseModel):
    student_answer: str = Field(min_length=1)
    standard_answer: str = Field(min_length=1)


class VerificationResult(SkillResult):
    correct: bool | None = None
    method: str | None = None
    normalized_student_answer: str | None = None
    normalized_standard_answer: str | None = None


class IndependentSolveInput(BaseModel):
    problem_text: str = Field(min_length=1)
    section_no: str = ""
    problem_no: str = ""


class IndependentSolveResult(SkillResult):
    answer: str | None = None
    full_solution: str | None = None
    model_name: str | None = None
    raw_response: dict = Field(default_factory=dict)


class MisconceptionDiagnosisInput(BaseModel):
    student_answer: str = Field(min_length=1)
    standard_answer: str = Field(min_length=1)
    problem_text: str = ""
    verification_correct: bool | None = None
    intermediate_steps: str = Field(default="", max_length=12000)


class DiagnosisItem(BaseModel):
    code: str
    label: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str
    next_step: str
    evidence_source: str = "final_answer"
    evidence_location: str = ""


class MisconceptionDiagnosisResult(SkillResult):
    diagnoses: list[DiagnosisItem] = Field(default_factory=list)
    summary: str


class EvidenceRetrievalInput(BaseModel):
    problem_id: str = Field(min_length=1)


class EvidenceRecord(BaseModel):
    section_no: str
    problem_no: str
    knowledge_points: list[str] = Field(default_factory=list)
    has_problem_image: bool = False
    has_full_solution: bool = False
    answer_status: str


class EvidenceRetrievalResult(SkillResult):
    record: EvidenceRecord | None = None


class AnswerPerceptionInput(BaseModel):
    image_base64: str = Field(min_length=100, max_length=12_000_000)
    problem_id: str = Field(min_length=1)
    problem_text: str = ""


class FormulaRegion(BaseModel):
    label: str = "formula"
    bbox: list[float] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class AnswerPerceptionResult(SkillResult):
    recognized_work: str | None = None
    formula_regions: list[FormulaRegion] = Field(default_factory=list)
    provider: str | None = None
    raw_response: dict = Field(default_factory=dict)
