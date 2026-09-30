from pathlib import Path
import re
import unicodedata
from docx import Document
from docx.document import Document as DocumentType
from docx.table import _Cell, Table
from docx.text.paragraph import Paragraph
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
import fitz
# =====================================================================
# COMMON HELPERS
# =====================================================================
def clean_powerpoint_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    invisible_characters = [
        "\u200b",
        "\u200c",
        "\u200d",
        "\u200e",
        "\u200f",
        "\u2060",
        "\ufeff",
        "\u00ad",
    ]
    for character in invisible_characters:
        text = text.replace(character, "")
    text = text.replace("\u00a0", " ")
    return text
# =====================================================================
# DOCX HELPERS
# =====================================================================
def iter_block_items(parent):

    if isinstance(parent, DocumentType):
        parent_element = parent.element.body

    elif isinstance(parent, _Cell):
        parent_element = parent._tc

    else:
        parent_element = parent._element

    for child in parent_element.iterchildren():

        if child.tag.endswith("}p"):
            yield Paragraph(child, parent)

        elif child.tag.endswith("}tbl"):
            yield Table(child, parent)


def iter_paragraphs(parent):

    for block in iter_block_items(parent):

        if isinstance(block, Paragraph):
            yield block

        elif isinstance(block, Table):

            processed_cells = set()

            for row in block.rows:
                for cell in row.cells:

                    cell_id = id(cell._tc)

                    if cell_id in processed_cells:
                        continue

                    processed_cells.add(cell_id)

                    yield from iter_paragraphs(cell)
def replace_text_in_word_paragraph(
    paragraph,
    old_text,
    new_text,
):

    runs = paragraph.runs

    if not runs:
        return 0

    full_text = "".join(run.text for run in runs)

    if old_text not in full_text:
        return 0

    match_positions = []
    search_position = 0

    while True:

        match_start = full_text.find(
            old_text,
            search_position
        )

        if match_start == -1:
            break

        match_positions.append(match_start)

        search_position = (
            match_start + len(old_text)
        )

    replacement_count = 0

    for match_start in reversed(match_positions):

        match_end = (
            match_start + len(old_text)
        )

        run_spans = []

        current_position = 0

        for run in runs:

            start = current_position
            end = start + len(run.text)

            run_spans.append({
                "run": run,
                "start": start,
                "end": end,
            })

            current_position = end

        affected_runs = [
            item
            for item in run_spans
            if item["start"] < match_end
            and item["end"] > match_start
        ]

        if not affected_runs:
            continue

        first_item = affected_runs[0]
        last_item = affected_runs[-1]

        first_run = first_item["run"]
        last_run = last_item["run"]

        start_inside_first = (
            match_start - first_item["start"]
        )

        end_inside_last = (
            match_end - last_item["start"]
        )

        text_before = first_run.text[:start_inside_first]
        text_after = last_run.text[end_inside_last:]

        if first_run is last_run:

            first_run.text = (
                text_before
                + new_text
                + text_after
            )

        else:

            first_run.text = (
                text_before + new_text
            )

            for item in affected_runs[1:-1]:
                item["run"].text = ""

            last_run.text = text_after

        replacement_count += 1

    return replacement_count


def replace_in_word_document(
    document,
    old_text,
    new_text,
):

    total_replacements = 0

    for paragraph in iter_paragraphs(document):

        total_replacements += (
            replace_text_in_word_paragraph(
                paragraph,
                old_text,
                new_text,
            )
        )

    processed_parts = set()

    for section in document.sections:

        header_footer_parts = [
            section.header,
            section.first_page_header,
            section.even_page_header,
            section.footer,
            section.first_page_footer,
            section.even_page_footer,
        ]

        for part in header_footer_parts:

            part_id = str(part.part.partname)

            if part_id in processed_parts:
                continue

            processed_parts.add(part_id)

            for paragraph in iter_paragraphs(part):

                total_replacements += (
                    replace_text_in_word_paragraph(
                        paragraph,
                        old_text,
                        new_text,
                    )
                )

    return total_replacements


# =====================================================================
# PPTX HELPERS
# =====================================================================

def replace_text_in_ppt_paragraph(
    paragraph,
    old_text,
    new_text,
):

    runs = list(paragraph.runs)

    if not runs:
        return 0

    full_text = "".join(
        run.text for run in runs
    )

    search_text = old_text

    if search_text not in full_text:

        cleaned_full = clean_powerpoint_text(
            full_text
        )

        cleaned_old = clean_powerpoint_text(
            old_text
        )

        if (
            cleaned_old in cleaned_full
            and len(cleaned_old) <= len(cleaned_full)
        ):
            full_text = cleaned_full
            search_text = cleaned_old
        else:
            return 0

    match_positions = []

    search_position = 0

    while True:

        match_start = full_text.find(
            search_text,
            search_position,
        )

        if match_start == -1:
            break

        match_positions.append(match_start)

        search_position = (
            match_start + len(search_text)
        )

    replacement_count = 0

    for match_start in reversed(match_positions):

        match_end = (
            match_start + len(search_text)
        )

        run_spans = []
        current_position = 0

        for run in runs:

            start = current_position
            end = start + len(run.text)

            run_spans.append({
                "run": run,
                "start": start,
                "end": end,
            })

            current_position = end

        affected_runs = [
            item
            for item in run_spans
            if item["start"] < match_end
            and item["end"] > match_start
        ]

        if not affected_runs:
            continue

        first_item = affected_runs[0]
        last_item = affected_runs[-1]

        first_run = first_item["run"]
        last_run = last_item["run"]

        start_inside_first = (
            match_start - first_item["start"]
        )

        end_inside_last = (
            match_end - last_item["start"]
        )

        text_before = first_run.text[:start_inside_first]
        text_after = last_run.text[end_inside_last:]

        if first_run is last_run:

            first_run.text = (
                text_before
                + new_text
                + text_after
            )

        else:

            first_run.text = (
                text_before + new_text
            )

            for item in affected_runs[1:-1]:
                item["run"].text = ""

            last_run.text = text_after

        replacement_count += 1

    return replacement_count


def replace_in_text_frame(
    text_frame,
    old_text,
    new_text,
):

    total = 0

    for paragraph in text_frame.paragraphs:

        total += replace_text_in_ppt_paragraph(
            paragraph,
            old_text,
            new_text,
        )

    return total


def replace_in_table(
    table,
    old_text,
    new_text,
):

    total = 0
    processed_cells = set()

    for row in table.rows:
        for cell in row.cells:

            cell_id = id(cell._tc)

            if cell_id in processed_cells:
                continue

            processed_cells.add(cell_id)

            total += replace_in_text_frame(
                cell.text_frame,
                old_text,
                new_text,
            )

    return total


def replace_in_shape(
    shape,
    old_text,
    new_text,
):

    total = 0

    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:

        for child in shape.shapes:

            total += replace_in_shape(
                child,
                old_text,
                new_text,
            )

        return total

    if getattr(shape, "has_table", False):

        return replace_in_table(
            shape.table,
            old_text,
            new_text,
        )

    if getattr(shape, "has_text_frame", False):

        total += replace_in_text_frame(
            shape.text_frame,
            old_text,
            new_text,
        )

    return total


# =====================================================================
# PUBLIC FUNCTIONS USED BY AUTO UPDATE AGENT
# =====================================================================

def update_docx_claim(
    file_path: str,
    old_claim: str,
    new_claim: str,
) -> int:

    document = Document(file_path)

    replacements = replace_in_word_document(
        document,
        old_claim,
        new_claim,
    )
    if replacements > 0:
        document.save(file_path)

    return replacements
def update_pdf_claim(
    file_path: str,
    page_number: int,
    rect,
    old_claim: str,
    new_claim: str,
) -> int:
    import fitz
    if len(new_claim.strip()) > len(old_claim.strip()):
        print(
            "❌ PDF UPDATE REJECTED"
        )
        return -1
    try:
        
        document = fitz.open(file_path)
        page = document[page_number]
        page.add_redact_annot(
            rect,
            fill=(1, 1, 1),
        )
        page.apply_redactions()
        result = page.insert_textbox(
            rect,
            new_claim,
        )
        print(
            f"PDF INSERT RESULT = {result}"
        )
        if result < 0:
            print("❌ PDF TEXT DOES NOT FIT IN ORIGINAL RECT")
            document.close()
            return -1
        temp_path = file_path.replace(".pdf", "_updated.pdf")
        document.save(temp_path)
        print(f"UPDATED PDF SAVED TO = {temp_path}")
        document.close()
        return temp_path
    except Exception as e:
        print(
            f"❌ PDF UPDATE FAILED: {e}"
        )
        return -1
def update_pptx_claim(
    file_path: str,
    old_claim: str,
    new_claim: str,
) -> int:

    presentation = Presentation(file_path)

    replacements = 0

    for slide in presentation.slides:

        for shape in slide.shapes:

            replacements += replace_in_shape(
                shape,
                old_claim,
                new_claim,
            )

    if replacements > 0:
        presentation.save(file_path)

    return replacements
