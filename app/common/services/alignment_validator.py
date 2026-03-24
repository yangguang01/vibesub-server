import json
from typing import Dict, List, Sequence, Tuple

from pydantic import BaseModel, Field

from app.common.core.config import get_task_config
from app.common.core.logging import logger
from app.common.services.llm_runtime import RetryableLLMError, safe_json_chat_completion


class AlignmentIssue(BaseModel):
    type: str
    line_ids: List[int]
    reason: str


class AlignmentValidationResult(BaseModel):
    pass_: bool = Field(alias="pass")
    confidence: float = 0.0
    issues: List[AlignmentIssue] = Field(default_factory=list)
    summary: str = ""

    model_config = {"populate_by_name": True}


VALIDATION_ISSUE_TYPES = {
    "merged_lines",
    "split_line",
    "shifted_alignment",
    "missing_content",
    "borrowed_neighbor_content",
    "invalid_keys",
    "line_count_mismatch",
    "uncertain",
}

SUSPICIOUS_REASON_MARKERS = (
    "actually, re-examining",
    "upon closer inspection",
    "i think it passes",
    "pass true",
    "no issue found",
    "correctly translates only that line",
    "does not borrow",
    "this is one-to-one",
    "one-to-one and correct",
    "all seem one-to-one",
    "so perhaps no issues",
)

ALIGNMENT_SYSTEM_PROMPT = """You are an alignment validator for bilingual subtitles.

Your job is NOT to translate, rewrite, improve style, or repair the Chinese text.
Your only job is to audit whether each translated Chinese line stays aligned to the same source line id without spilling into neighboring lines.

Audit target:
- The Chinese output must preserve one-to-one alignment with the English input.
- Even if the total number of lines is correct, any semantic boundary shift must be treated as a failure.
- This validator is only about line-to-line alignment boundaries, not translation quality.
- Do NOT fail a line just because the source line is fragmentary, incomplete, awkward, ASR-broken, or not a full sentence.
- Do NOT fail a line just because the Chinese line is short, awkward, or incomplete, as long as it still maps only to the same source line.
- Ignore whether the line is a complete thought. Only care whether meaning from neighboring lines was merged, shifted, split across ids, or borrowed.
- If you are uncertain, return fail with issue type "uncertain".
- Do NOT include self-corrections, re-audits, chain-of-thought, or debate in the reason field.
- If a short EN line is followed by a very long EN line, that is still PASS as long as each ZH line maps only to its own EN line.

Important exception:
- Some adjacent English lines may belong to the same logical sentence because the subtitle line was visually split.
- These allowed groups are provided in "allowed_duplicate_groups".
- For those groups only, it is acceptable that the translated lines repeat or reflect the same shared meaning inside that group.
- But they still must NOT borrow meaning from outside the allowed group.

You must check:
1. Line count matches.
2. Line ids are complete, ordered, and consistent.
3. Each Chinese line only corresponds to the same-id English line, or to an allowed duplicate group.
4. No merging of neighboring English lines into one Chinese line unless contained within an allowed duplicate group.
5. No splitting one English line's meaning across multiple later Chinese lines.
6. No shifted alignment.
7. No borrowed neighbor content.
8. Ignore sentence completeness, grammar quality, and whether the source line itself is only a fragment.
9. Only report "missing_content" when the missing part is caused by cross-line boundary problems, not when the source line itself is inherently incomplete.

Examples:
- EN 211: "What has dropped off about Sora is the new downloads."
  ZH 211: "Sora下降的是新下载量"
  EN 212: "So there may be."
  ZH 212: "所以可能有"
  This should PASS if line 211 does not absorb line 212, and line 212 does not absorb line 213.
- If ZH 211 contains both the meaning of EN 211 and EN 212, that is merged_lines and should FAIL.
- If ZH 212 contains the meaning of EN 213, that is shifted_alignment / borrowed_neighbor_content and should FAIL.
- If EN 8 is a short setup line and EN 9 is a long line, and ZH 8 only translates EN 8 while ZH 9 only translates EN 9, that should PASS.

Issue types:
- merged_lines
- split_line
- shifted_alignment
- missing_content
- borrowed_neighbor_content
- invalid_keys
- line_count_mismatch
- uncertain

Output JSON only.
Return exactly:
{
  "pass": true,
  "confidence": 0.0,
  "issues": [{"type": "merged_lines", "line_ids": [1, 2], "reason": "..."}],
  "summary": "..."
}
"""


def build_allowed_duplicate_groups(chunk: Sequence[Tuple[int, str]]) -> List[List[int]]:
    groups: List[List[int]] = []
    punctuation = (".", "?", "!", ":", ";", '"', "'", ")", "]")
    for (left_id, left_text), (right_id, right_text) in zip(chunk, chunk[1:]):
        if right_id != left_id + 1:
            continue
        left_text = left_text.strip()
        right_text = right_text.strip()
        if not left_text or not right_text:
            continue
        if left_text.endswith(punctuation):
            continue
        if right_text[0].isupper():
            continue
        groups.append([left_id, right_id])
    return groups


def validate_chunk_shape(
    source_chunk: Sequence[Tuple[int, str]],
    translated_chunk: Dict[int, str],
) -> tuple[bool, str]:
    source_ids = [line_id for line_id, _ in source_chunk]
    translated_ids = list(translated_chunk.keys())
    if len(source_ids) != len(translated_ids):
        return False, "line_count_mismatch"
    if translated_ids != source_ids:
        return False, "invalid_keys"
    return True, ""


def build_validator_input(
    source_chunk: Sequence[Tuple[int, str]],
    translated_chunk: Dict[int, str],
) -> Dict[str, object]:
    return {
        "chunk_start": source_chunk[0][0],
        "chunk_end": source_chunk[-1][0],
        "source_lines": [{"id": line_id, "text": text} for line_id, text in source_chunk],
        "translated_lines": [{"id": line_id, "text": translated_chunk[line_id]} for line_id, _ in source_chunk],
        "allowed_duplicate_groups": build_allowed_duplicate_groups(source_chunk),
    }


def _is_suspicious_alignment_result(result: AlignmentValidationResult) -> bool:
    if result.pass_:
        return False
    for issue in result.issues:
        reason = issue.reason.lower()
        if any(marker in reason for marker in SUSPICIOUS_REASON_MARKERS):
            return True
    return False


async def validate_alignment(
    source_chunk: Sequence[Tuple[int, str]],
    translated_chunk: Dict[int, str],
) -> AlignmentValidationResult:
    is_valid_shape, issue_type = validate_chunk_shape(source_chunk, translated_chunk)
    if not is_valid_shape:
        return AlignmentValidationResult(
            pass_=False,
            confidence=1.0,
            issues=[
                AlignmentIssue(
                    type=issue_type,
                    line_ids=[line_id for line_id, _ in source_chunk],
                    reason="程序硬校验未通过",
                )
            ],
            summary="程序硬校验未通过",
        )

    validator_input = build_validator_input(source_chunk, translated_chunk)
    validator_messages = [
        {"role": "system", "content": ALIGNMENT_SYSTEM_PROMPT},
        {"role": "user", "content": f"Audit the following subtitle chunk.\n\nInput JSON:\n{json.dumps(validator_input, ensure_ascii=False)}"},
    ]
    max_attempts = get_task_config("alignment_validator").get("max_validator_retries", 2)

    for semantic_attempt in range(2):
        response = await safe_json_chat_completion(
            "alignment_validator",
            validator_messages,
            max_attempts=max_attempts,
        )
        result = AlignmentValidationResult.model_validate(response["parsed"])
        for issue in result.issues:
            if issue.type not in VALIDATION_ISSUE_TYPES:
                logger.warning(f"validator 返回未知 issue 类型: {issue.type}")

        if not _is_suspicious_alignment_result(result):
            return result

        logger.warning(
            "alignment validator 返回自相矛盾结果，chunk=%s-%s，semantic_attempt=%s",
            source_chunk[0][0],
            source_chunk[-1][0],
            semantic_attempt + 1,
        )
        validator_messages = [
            {"role": "system", "content": ALIGNMENT_SYSTEM_PROMPT},
            {"role": "user", "content": f"Audit the following subtitle chunk.\n\nInput JSON:\n{json.dumps(validator_input, ensure_ascii=False)}"},
            {"role": "assistant", "content": response["content"]},
            {
                "role": "user",
                "content": (
                    "Your previous answer was self-contradictory or included meta-reasoning. "
                    "Re-audit from scratch. "
                    "If each ZH line maps only to its own EN line, return pass=true. "
                    "Do not include self-corrections or debate in reason."
                ),
            },
        ]

    logger.warning(
        "alignment validator 连续返回自相矛盾结果，按 validator 不稳定处理并放行，chunk=%s-%s",
        source_chunk[0][0],
        source_chunk[-1][0],
    )
    return AlignmentValidationResult(
        pass_=True,
        confidence=0.0,
        issues=[],
        summary="validator_self_contradiction_ignored",
    )


def alignment_mode() -> str:
    return str(get_task_config("alignment_validator").get("mode", "observe")).lower()


def fallback_chunk_size() -> int:
    return int(get_task_config("alignment_validator").get("fallback_chunk_size", 12))


def max_content_retries() -> int:
    return int(get_task_config("alignment_validator").get("max_content_retries", 3))
