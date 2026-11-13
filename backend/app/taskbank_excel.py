"""T1.5 workbook template and in-memory preflight; no storage or database writes."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.spreadsheet_drawing import OneCellAnchor, TwoCellAnchor
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from PIL import Image as PillowImage

from app.taskbank import RevisionContent

SHEET = "题库导入"
HEADERS = (
    "题目编号",
    "学段",
    "题型",
    "排序号",
    "测试员引导语",
    "题目内容",
    "目标对话轮数",
    "选项 A 图片",
    "选项 B 图片",
    "选项 C 图片",
    "题目图片",
    "材料说明",
    "参考答案",
    "评分说明",
    "AI 追问范围",
)
SLOTS = {8: "option_A", 9: "option_B", 10: "option_C", 11: "question_image"}
TYPE_LABELS = {
    "选择题": "instruction",
    "跟读题": "repetition",
    "问答题": "qa",
    "对话题": "dialogue",
    "说话题": "speaking",
}


@dataclass(frozen=True)
class Issue:
    message: str
    row: int | None = None
    column: str | None = None
    severity: str = "error"


@dataclass(frozen=True)
class ImageData:
    data: bytes
    sha256: str
    media_type: str


@dataclass(frozen=True)
class PreviewRow:
    row: int
    task_code: str
    display_order: int | None
    content: RevisionContent
    images: dict[str, ImageData]


@dataclass
class Preview:
    rows: list[PreviewRow] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


def create_template() -> bytes:
    """Return an editable workbook with the confirmed 15-column layout."""
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    ws.append(HEADERS)
    ws.freeze_panes = "A2"
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True)
    ws.column_dimensions["A"].number_format = "@"
    for col in "ABCDEFGHIJKLMNO":
        ws.column_dimensions[col].width = 20
    instructions = wb.create_sheet("填写说明")
    for line in (
        "VNext T1.5：题库导入表 A:O 共 15 列；不要改变表头及列序。",
        "题目编号使用文本，保留前导零；同一题库内题号唯一。",
        "学段、题型与题目内容必填；题型可填内部标识或选择题、跟读题、问答题、对话题、说话题。",
        "排序号可为空；对话题目标对话轮数为正整数，不设置默认值。",
        "插入嵌入式图片并锚定在同一题所在行的 H/I/J/K 单元格；不要仅填写图片文件路径。",
        "选择题 H/I/J 分别为 A/B/C 图片，参考答案 M 填写已有选项的 A、B 或 C。",
        "L 材料说明、M 参考答案、N 评分说明、O AI 追问范围按题目需要填写。",
        "空白可选字段表示清空；旧版 16/19 列模板需要显式迁移，不能直接导入。",
    ):
        instructions.append([line])
    instructions.column_dimensions["A"].width = 100
    output = BytesIO()
    wb.save(output)
    wb.close()
    return output.getvalue()


def _integer(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("应为整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise TypeError("应为整数")


def _text(value: object, required: bool = False) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError("不能为空")
        return None
    if not isinstance(value, str):
        raise TypeError("应为文本")
    return value


def preflight_xlsx(data: bytes) -> Preview:
    """Parse rows and embedded images without persisting assets or inferring DB state."""
    result = Preview()
    try:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            wb = load_workbook(BytesIO(data), read_only=False, data_only=False)
    except (BadZipFile, OSError, ValueError, KeyError, ParseError) as exc:
        result.issues.append(Issue(f"无法读取 XLSX：{type(exc).__name__}"))
        return result
    try:
        for warning in captured:
            message = str(warning.message)
            if "image" in message.lower() or "drawing" in message.lower():
                result.issues.append(Issue(f"无法完整读取工作簿图片：{message}"))
        if SHEET not in wb.sheetnames:
            result.issues.append(Issue("缺少“题库导入”工作表"))
            return result
        ws = wb[SHEET]
        actual = [ws.cell(1, col).value for col in range(1, ws.max_column + 1)]
        while actual and actual[-1] is None:
            actual.pop()
        if tuple(actual) != HEADERS:
            if len(actual) in (16, 19):
                result.issues.append(
                    Issue(f"检测到 {len(actual)} 列模板，旧版模板须显式迁移至 T1.5 的 A:O 15 列")
                )
            else:
                result.issues.append(Issue("表头或列序不符合 T1.5 的 A:O 15 列模板"))
            return result
        images_by_row: dict[int, dict[str, ImageData]] = {}
        # openpyxl exposes embedded images through its worksheet drawing collection.
        for image in ws._images:
            anchor = image.anchor
            if not isinstance(anchor, (OneCellAnchor, TwoCellAnchor)):
                result.issues.append(Issue("图片必须锚定到题目行的 H/I/J/K 单元格"))
                continue
            row, col = anchor._from.row + 1, anchor._from.col + 1
            slot = SLOTS.get(col)
            if row < 2 or slot is None:
                result.issues.append(
                    Issue(
                        "图片锚点不在题目行的 H/I/J/K 单元格",
                        row,
                        get_column_letter(col) if 1 <= col <= 16384 else str(col),
                    )
                )
                continue
            row_images = images_by_row.setdefault(row, {})
            if slot in row_images:
                result.issues.append(Issue("同一单元格存在多张图片，无法确定归属", row, slot))
                continue
            try:
                image_bytes = image._data()
                with PillowImage.open(BytesIO(image_bytes)) as im:
                    media_type = PillowImage.MIME.get(im.format)
                    im.verify()
                if media_type is None:
                    raise ValueError("未知图片格式")
            except (OSError, ValueError, SyntaxError):
                result.issues.append(Issue("图片数据无法读取或格式不受支持", row, slot))
                continue
            row_images[slot] = ImageData(image_bytes, sha256(image_bytes).hexdigest(), media_type)
        seen: dict[str, PreviewRow] = {}
        for row_num in range(2, max(ws.max_row, max(images_by_row, default=1)) + 1):
            cells = [ws.cell(row_num, col) for col in range(1, 16)]
            images = images_by_row.get(row_num, {})
            extra_cells = ws[row_num][15:]
            if not images and all(cell.value is None for cell in cells + list(extra_cells)):
                continue
            row_start = len(result.issues)
            values = [None] * 15
            for idx, cell in enumerate(cells):
                if cell.data_type in ("f", "e"):
                    result.issues.append(
                        Issue(
                            "不接受公式或错误值，请填写明确的单元格值", row_num, cell.column_letter
                        )
                    )
                    continue
                if idx in (7, 8, 9, 10):
                    if cell.value is not None:
                        result.issues.append(
                            Issue(
                                "图片列必须使用嵌入图片，不能填写单元格内容",
                                row_num,
                                cell.column_letter,
                            )
                        )
                    continue
                try:
                    if idx in (3, 6):
                        values[idx] = _integer(cell.value)
                    else:
                        values[idx] = _text(cell.value, required=idx in (0, 1, 2, 5))
                except (ValueError, TypeError) as exc:
                    result.issues.append(Issue(str(exc), row_num, cell.column_letter))
            if any(cell.value is not None for cell in extra_cells):
                result.issues.append(Issue("O 列之后存在数据，请检查模板列序", row_num))
            if len(result.issues) != row_start:
                continue
            code = values[0].strip()
            raw_type = values[2].strip()
            task_type = TYPE_LABELS.get(
                raw_type, raw_type.lower() if raw_type.lower() in TYPE_LABELS.values() else raw_type
            )
            rounds = values[6]
            if task_type == "dialogue" and (rounds is None or rounds <= 0):
                result.issues.append(Issue("对话题目标轮数必须为正整数", row_num, "G"))
            elif rounds is not None and rounds <= 0:
                result.issues.append(Issue("目标轮数必须为正整数", row_num, "G"))
            answer = values[12]
            if task_type == "instruction":
                answer = answer.strip().upper() if answer else None
                if any(slot not in images for slot in ("option_A", "option_B", "option_C")):
                    result.issues.append(Issue("选择题须提供 A、B、C 三张选项图片", row_num, "H:J"))
                if answer not in ("A", "B", "C") or "option_" + answer not in images:
                    result.issues.append(
                        Issue("参考答案必须为对应已有选项图片的 A、B 或 C", row_num, "M")
                    )
            if len(result.issues) != row_start:
                continue
            row = PreviewRow(
                row_num,
                code,
                values[3],
                RevisionContent(
                    grade_level=values[1],
                    task_type=task_type,
                    guide_text=values[4],
                    prompt_text=values[5],
                    target_rounds=rounds,
                    material_description=values[11],
                    reference_answer=answer,
                    scoring_instruction=values[13],
                    ai_followup_scope=values[14],
                ),
                images,
            )
            prior = seen.get(code)
            if prior is not None:
                same = (
                    prior.content == row.content
                    and prior.display_order == row.display_order
                    and {k: v.sha256 for k, v in prior.images.items()}
                    == {k: v.sha256 for k, v in row.images.items()}
                )
                result.issues.append(
                    Issue(
                        f"题号 {code} 与第 {prior.row} 行重复"
                        + ("，内容相同已合并" if same else "，内容或排序冲突"),
                        row_num,
                        "A",
                        "warning" if same else "error",
                    )
                )
            else:
                seen[code] = row
                result.rows.append(row)
        return result
    finally:
        wb.close()
