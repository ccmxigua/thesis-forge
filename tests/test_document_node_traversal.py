"""Streaming traversal must not confuse recycled lxml proxy addresses."""
from pathlib import Path
import sys

from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from docx_semantics import all_table_paragraphs


def test_streaming_table_reads_every_cell_after_proxy_recycling(tmp_path):
    source = tmp_path / 'cells.docx'
    doc = Document()
    table = doc.add_table(rows=30, cols=3)
    for index, cell in enumerate(table._cells):
        cell.text = f'cell-{index}'
    doc.save(source)
    reloaded = Document(source)
    # Consume plain strings, not Paragraphs/DocumentNodes retaining old proxies.
    actual = [paragraph.text for paragraph in all_table_paragraphs(reloaded)]
    assert actual == [f'cell-{index}' for index in range(90)]


def test_merged_cell_is_visited_once(tmp_path):
    source = tmp_path / 'merged.docx'
    doc = Document()
    table = doc.add_table(rows=1, cols=3)
    table.cell(0, 0).merge(table.cell(0, 1)).text = 'merged'
    table.cell(0, 2).text = 'tail'
    doc.save(source)
    assert [p.text for p in all_table_paragraphs(Document(source))] == ['merged', 'tail']


def test_nested_and_later_tables_preserve_streaming_xml_order(tmp_path):
    source = tmp_path / 'nested.docx'
    doc = Document()
    outer = doc.add_table(rows=1, cols=2)
    outer.cell(0, 0).text = 'outer-left'
    outer.cell(0, 1).text = 'outer-right'
    nested = outer.cell(0, 0).add_table(rows=2, cols=2)
    for index, cell in enumerate(nested._cells):
        cell.text = f'nested-{index}'
    later = doc.add_table(rows=2, cols=2)
    for index, cell in enumerate(later._cells):
        cell.text = f'later-{index}'
    doc.save(source)
    actual = [p.text for p in all_table_paragraphs(Document(source)) if p.text]
    assert actual == ['outer-left', *[f'nested-{i}' for i in range(4)],
                      'outer-right', *[f'later-{i}' for i in range(4)]]
