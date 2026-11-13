from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile

import pytest
from openpyxl import load_workbook
from openpyxl.drawing.image import Image
from openpyxl.drawing.spreadsheet_drawing import AbsoluteAnchor, AnchorMarker, TwoCellAnchor
from openpyxl.drawing.xdr import XDRPoint2D, XDRPositiveSize2D
from PIL import Image as PillowImage

from app.taskbank_excel import HEADERS, create_template, preflight_xlsx


def image_bytes(color):
    result = BytesIO()
    PillowImage.new("RGB", (3, 3), color).save(result, format="PNG")
    return result.getvalue()


def workbook():
    return load_workbook(BytesIO(create_template()))


def add_image(ws, cell, color="red", anchor=None):
    data = image_bytes(color)
    image = Image(BytesIO(data))
    image.anchor = anchor if anchor is not None else cell
    ws.add_image(image)
    return data


def encode(wb):
    output = BytesIO()
    wb.save(output)
    wb.close()
    return output.getvalue()


def row(ws, number=2, code="0001", kind="选择题"):
    for col, value in {
        1: code,
        2: "未预设学段",
        3: kind,
        4: 42,
        5: "引导",
        6: "题干",
        12: "材料",
        13: "B" if kind == "选择题" else "参考",
        14: "评分",
        15: "追问",
    }.items():
        ws.cell(number, col, value)


def issue_text(result):
    return " | ".join(i.message for i in result.issues)


def test_template_layout_and_example_roundtrip():
    wb = workbook()
    assert wb.sheetnames == ["题库导入", "填写说明"]
    ws = wb["题库导入"]
    assert tuple(cell.value for cell in ws[1]) == HEADERS
    assert ws.column_dimensions["A"].number_format == "@"
    assert wb["填写说明"].max_row >= 1
    preview = preflight_xlsx(encode(wb))
    assert preview.valid and not preview.rows


def test_embedded_images_in_empty_cells_keep_row_slot_and_content():
    wb = workbook()
    ws = wb["题库导入"]
    row(ws, 2)
    row(ws, 3, code="0002", kind="问答题")
    expected = {
        "option_A": add_image(ws, "H2", "red"),
        "option_B": add_image(ws, "I2", "green"),
        "option_C": add_image(ws, "J2", "blue"),
        "question_image": add_image(ws, "K2", "yellow"),
    }
    second = add_image(ws, "K3", "purple")
    assert all(ws[f"{col}2"].value is None for col in "HIJK")
    parsed = preflight_xlsx(encode(wb))
    assert parsed.valid, parsed.issues
    a, b = parsed.rows
    assert (a.row, a.task_code, a.display_order, a.content.task_type) == (
        2,
        "0001",
        42,
        "instruction",
    )
    assert (
        a.content.prompt_text,
        a.content.material_description,
        a.content.reference_answer,
        a.content.scoring_instruction,
        a.content.ai_followup_scope,
    ) == ("题干", "材料", "B", "评分", "追问")
    assert {k: v.data for k, v in a.images.items()} == expected
    assert all(
        v.sha256 == sha256(v.data).hexdigest() and v.media_type == "image/png"
        for v in a.images.values()
    )
    assert b.task_code == "0002" and b.images["question_image"].data == second


def test_two_cell_anchor_uses_top_left():
    wb = workbook()
    ws = wb["题库导入"]
    row(ws, 2, kind="问答题")
    marker = TwoCellAnchor(_from=AnchorMarker(col=10, row=1), to=AnchorMarker(col=11, row=2))
    data = add_image(ws, "K2", anchor=marker)
    result = preflight_xlsx(encode(wb))
    assert result.valid and result.rows[0].images["question_image"].data == data


@pytest.mark.parametrize(
    "column,value,expected",
    [
        (1, 123, "应为文本"),
        (2, 1, "应为文本"),
        (4, "2", "应为整数"),
        (6, None, "不能为空"),
        (7, 1.5, "应为整数"),
        (13, True, "应为文本"),
    ],
)
def test_field_types_rejected(column, value, expected):
    wb = workbook()
    ws = wb["题库导入"]
    row(ws, kind="问答题")
    ws.cell(2, column).value = value
    result = preflight_xlsx(encode(wb))
    assert not result.valid and expected in issue_text(result)


@pytest.mark.parametrize("rounds", [None, 0, -2, 1.2, True])
def test_dialogue_requires_positive_integer(rounds):
    wb = workbook()
    ws = wb["题库导入"]
    row(ws, kind="对话题")
    ws["G2"] = rounds
    assert not preflight_xlsx(encode(wb)).valid


def test_dialogue_accepts_large_round_count_and_unknown_grade():
    wb = workbook()
    ws = wb["题库导入"]
    row(ws, kind="对话题")
    ws["G2"] = 77
    result = preflight_xlsx(encode(wb))
    assert result.valid and result.rows[0].content.target_rounds == 77
    assert result.rows[0].content.grade_level == "未预设学段"


def test_choice_correct_answer_must_match_real_images():
    wb = workbook()
    ws = wb["题库导入"]
    row(ws)
    add_image(ws, "H2")
    add_image(ws, "J2")
    result = preflight_xlsx(encode(wb))
    assert not result.valid and "对应已有选项图片" in issue_text(result)


def test_wrong_location_or_orphan_image_is_error():
    wb = workbook()
    ws = wb["题库导入"]
    row(ws, kind="问答题")
    add_image(ws, "L2")
    add_image(ws, "K4")
    result = preflight_xlsx(encode(wb))
    assert not result.valid and "锚点" in issue_text(result)
    assert any(i.row == 4 and i.column == "A" for i in result.issues)


def test_absolute_anchor_rejected():
    wb = workbook()
    ws = wb["题库导入"]
    row(ws, kind="问答题")
    anchor = AbsoluteAnchor(pos=XDRPoint2D(0, 0), ext=XDRPositiveSize2D(10000, 10000))
    add_image(ws, "K2", anchor=anchor)
    result = preflight_xlsx(encode(wb))
    assert not result.valid and "锚定" in issue_text(result)


def test_multiple_images_in_one_cell_are_rejected():
    wb = workbook()
    ws = wb["题库导入"]
    row(ws, kind="问答题")
    add_image(ws, "K2", "red")
    add_image(ws, "K2", "blue")
    result = preflight_xlsx(encode(wb))
    assert not result.valid and "多张图片" in issue_text(result)


@pytest.mark.parametrize("size", [16, 19])
def test_old_templates_are_rejected(size):
    wb = workbook()
    ws = wb["题库导入"]
    ws.cell(1, size, "旧列")
    result = preflight_xlsx(encode(wb))
    assert not result.valid and f"{size} 列" in issue_text(result)


def test_formula_wrong_header_and_bad_file_are_rejected():
    wb = workbook()
    ws = wb["题库导入"]
    row(ws, kind="问答题")
    ws["F2"] = "=1+1"
    assert "公式" in issue_text(preflight_xlsx(encode(wb)))
    wb = workbook()
    wb["题库导入"]["K1"] = "旧正确选项"
    assert "表头" in issue_text(preflight_xlsx(encode(wb)))
    assert not preflight_xlsx(b"not an xlsx").valid


def test_duplicate_identical_merges_but_changed_images_or_order_conflict():
    for mode in ("identical", "image", "order"):
        wb = workbook()
        ws = wb["题库导入"]
        row(ws, 2, kind="问答题")
        row(ws, 3, kind="问答题")
        add_image(ws, "K2", "red")
        add_image(ws, "K3", "blue" if mode == "image" else "red")
        if mode == "order":
            ws["D3"] = 43
        result = preflight_xlsx(encode(wb))
        assert len(result.rows) == 1 and result.issues[0].row == 3
        assert result.valid == (mode == "identical")
        assert ("已合并" if mode == "identical" else "冲突") in issue_text(result)


def test_nonstandard_type_does_not_create_type_whitelist():
    wb = workbook()
    row(wb["题库导入"], kind="自定义题型")
    result = preflight_xlsx(encode(wb))
    assert result.valid and result.rows[0].content.task_type == "自定义题型"


def test_extra_data_without_other_row_values_is_not_ignored():
    wb = workbook()
    wb["题库导入"]["P4"] = "意外数据"
    result = preflight_xlsx(encode(wb))
    assert not result.valid and any(i.row == 4 and "O 列之后" in i.message for i in result.issues)


def test_known_type_identifiers_are_normalized_before_validation():
    wb = workbook()
    row(wb["题库导入"], kind="DIALOGUE")
    result = preflight_xlsx(encode(wb))
    assert not result.valid and "对话题目标轮数" in issue_text(result)


def test_unreadable_embedded_image_is_not_silently_ignored():
    wb = workbook()
    ws = wb["题库导入"]
    row(ws, kind="问答题")
    add_image(ws, "K2")
    corrupted = BytesIO()
    with ZipFile(BytesIO(encode(wb))) as source, ZipFile(corrupted, "w") as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename.startswith("xl/media/"):
                data = b"invalid image"
            target.writestr(item, data)
    result = preflight_xlsx(corrupted.getvalue())
    assert not result.valid and "图片" in issue_text(result)


def test_picture_column_cannot_be_replaced_with_a_path():
    wb = workbook()
    ws = wb["题库导入"]
    row(ws, kind="问答题")
    ws["K2"] = "local-image.png"
    result = preflight_xlsx(encode(wb))
    assert not result.valid and any(i.column == "K" and i.row == 2 for i in result.issues)
