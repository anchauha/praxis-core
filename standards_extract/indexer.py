#!/usr/bin/env python3
"""
Indiana Academic Standards Indexer (Mathematics & Science)

Reads course_manifest.json, parses every document that has a parser, and writes
standards_index.json. The manifest decides which documents are in scope, what
each course is called, and which published version it is; this file only reads
the PDFs. Run build_manifest.py first.

Every record gets a composite standard_key, because a printed code alone is not
unique across courses.
"""

import os
import sys
import glob
import collections
import hashlib
import re
import json
import shutil
from datetime import datetime
import fitz  # PyMuPDF

# Ensure UTF-8 output
sys.stdout.reconfigure(encoding='utf-8')


def parse_math_pdf(pdf_path, base_dir):
    """Parses an Indiana Academic Standards Mathematics PDF."""
    doc = fitz.open(pdf_path)
    filename = os.path.basename(pdf_path)
    rel_path = os.path.relpath(pdf_path, base_dir).replace('\\', '/')

    name_lower = filename.lower()
    if 'algebra-i-' in name_lower or name_lower.endswith('algebra-i-.pdf'):
        course_name = 'Algebra I'
        grade_band = 'High School'
    elif 'analytical-algebra-ii' in name_lower:
        course_name = 'Analytical Algebra II'
        grade_band = 'High School'
    elif 'algebra-ii' in name_lower:
        course_name = 'Algebra II'
        grade_band = 'High School'
    elif 'geometry' in name_lower:
        course_name = 'Geometry'
        grade_band = 'High School'
    elif 'grade-5' in name_lower:
        course_name = 'Grade 5'
        grade_band = 'Elementary (Grade 5)'
    elif 'grade-6' in name_lower:
        course_name = 'Grade 6'
        grade_band = 'Middle School (Grades 6-8)'
    elif 'grade-7' in name_lower:
        course_name = 'Grade 7'
        grade_band = 'Middle School (Grades 6-8)'
    elif 'grade-8' in name_lower:
        course_name = 'Grade 8'
        grade_band = 'Middle School (Grades 6-8)'
    else:
        course_name = filename.replace('indiana-academic-standards-', '').replace('.pdf', '').replace('-', ' ').title()
        grade_band = 'Mathematics'

    standards = []

    # 1. Process Standards (Pages 4-5)
    for pno in range(3, min(5, len(doc))):
        text = doc[pno].get_text('text')
        matches = list(re.finditer(r'PS\.(\d+):\s*([^\n]+)', text))
        for i, m in enumerate(matches):
            ps_num = m.group(1)
            ps_title = re.sub(r'\s+', ' ', m.group(2)).strip()
            ps_id = f'PS.{ps_num}'
            start_pos = m.end()
            end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            desc = text[start_pos:end_pos].strip()
            desc = re.sub(r'\n\s*\d+\s*$', '', desc)
            desc = re.sub(r'\s+', ' ', desc).strip()

            if not any(s['id'] == ps_id for s in standards):
                standards.append({
                    'id': ps_id,
                    'subject': 'Mathematics',
                    'grade_or_course': course_name,
                    'grade_band': grade_band,
                    'standard_type': 'Process Standard',
                    'domain': 'Mathematics Process Standards',
                    'learning_outcome': None,
                    'title': ps_title,
                    'description': f"{ps_title.rstrip('.')}. {desc}".strip(),
                    'is_essential': False,
                    'clarification_statement': None,
                    'science_dimensions': None,
                    'source_file': rel_path,
                    'page_number': pno + 1
                })

    # 2. Content Standards (Page 6 onwards)
    current_domain = 'General'
    current_learning_outcome = None

    id_re = r'^((?:\d+|[A-Za-z0-9]+)\.[A-Za-z]+\.\d+)\.?$'
    course_headers = {'Algebra I', 'Algebra II', 'Analytical Algebra II', 'Geometry',
                      f'{course_name} Mathematics', course_name}

    # Flatten all content pages into a single (line, page_number) stream so that a
    # description or learning outcome which spans a page break is not lost, and running
    # headers/footers between the two halves can be skipped over.
    entries = []
    for pno in range(5, len(doc)):
        for raw in doc[pno].get_text('text').split('\n'):
            ls = raw.strip()
            if ls:
                entries.append((ls, pno + 1))

    def is_noise(s, pg):
        return (s == str(pg)
                or '2023 Indiana Academic Standards' in s
                or 'Standards identified as essential' in s)

    i = 0
    while i < len(entries):
        line, page = entries[i]

        if is_noise(line, page):
            i += 1
            continue

        # Check for Learning Outcome
        if line.startswith('Learning Outcome:'):
            lo_text = line.replace('Learning Outcome:', '').strip()
            # Domain heading is the nearest preceding non-noise line.
            p = i - 1
            while p >= 0 and is_noise(entries[p][0], entries[p][1]):
                p -= 1
            if p >= 0 and entries[p][0] not in course_headers:
                current_domain = entries[p][0]
            j = i + 1
            while j < len(entries) and not re.match(id_re, entries[j][0]) and not entries[j][0].startswith('Learning Outcome:'):
                if j + 1 < len(entries) and entries[j + 1][0].startswith('Learning Outcome:'):
                    break
                if not is_noise(entries[j][0], entries[j][1]):
                    lo_text += ' ' + entries[j][0]
                j += 1
            current_learning_outcome = re.sub(r'\s+', ' ', lo_text).strip()
            i = j
            continue

        # Check for standard ID, e.g. 5.NS.1, AI.NF.1, AII.ASE.1, G.GF.1
        m_id = re.match(id_re, line)
        if m_id:
            std_id = m_id.group(1)
            # Capture the domain / learning outcome for THIS standard before scanning
            # its description, since the scan may detect (and advance to) the next
            # domain heading that follows immediately on the same page.
            domain_for_std = current_domain
            lo_for_std = current_learning_outcome
            std_page = page
            desc_lines = []
            j = i + 1
            while j < len(entries):
                next_line = entries[j][0]
                if re.match(id_re, next_line):
                    break
                if next_line.startswith('Learning Outcome:'):
                    if desc_lines:
                        last_desc = desc_lines[-1]
                        if len(last_desc) < 60 and not last_desc.endswith('.'):
                            current_domain = last_desc
                            desc_lines.pop()
                    break
                if j + 1 < len(entries) and entries[j + 1][0].startswith('Learning Outcome:'):
                    current_domain = next_line
                    j += 1
                    break
                if is_noise(next_line, entries[j][1]):
                    j += 1
                    continue
                desc_lines.append(next_line)
                j += 1

            desc_full = re.sub(r'\s+', ' ', ' '.join(desc_lines)).strip()

            is_essential = False
            if '(E)' in desc_full:
                is_essential = True
                desc_full = desc_full.replace('(E)', '').strip()

            standards.append({
                'id': std_id,
                'subject': 'Mathematics',
                'grade_or_course': course_name,
                'grade_band': grade_band,
                'standard_type': 'Content Standard',
                'domain': domain_for_std,
                'learning_outcome': lo_for_std,
                'title': None,
                'description': desc_full,
                'is_essential': is_essential,
                'clarification_statement': None,
                'science_dimensions': None,
                'source_file': rel_path,
                'page_number': std_page
            })
            i = j
            continue

        i += 1

    return course_name, grade_band, standards


def parse_science_pdf(pdf_path, base_dir):
    """Parses an Indiana Academic Standards Science PDF."""
    doc = fitz.open(pdf_path)
    filename = os.path.basename(pdf_path)
    rel_path = os.path.relpath(pdf_path, base_dir).replace('\\', '/')

    name_lower = filename.lower()
    if 'grade-5' in name_lower:
        course_name = 'Grade 5'
        grade_band = 'Elementary (Grade 5)'
    elif 'grade-6' in name_lower:
        course_name = 'Grade 6'
        grade_band = 'Middle School (Grades 6-8)'
    elif 'grade-7' in name_lower:
        course_name = 'Grade 7'
        grade_band = 'Middle School (Grades 6-8)'
    elif 'grade-8' in name_lower:
        course_name = 'Grade 8'
        grade_band = 'Middle School (Grades 6-8)'
    elif 'biology' in name_lower:
        course_name = 'Biology'
        grade_band = 'High School'
    elif 'chemistry' in name_lower and 'integrated' not in name_lower:
        course_name = 'Chemistry'
        grade_band = 'High School'
    elif 'earth-and-space-science' in name_lower:
        course_name = 'Earth and Space Science'
        grade_band = 'High School'
    elif 'integrated-chemistry' in name_lower:
        course_name = 'Integrated Chemistry and Physics'
        grade_band = 'High School'
    elif 'physics-i' in name_lower:
        course_name = 'Physics I'
        grade_band = 'High School'
    else:
        course_name = filename.replace('indiana-academic-standards-', '').replace('.pdf', '').replace('-', ' ').title()
        grade_band = 'Science'

    standards = []

    # Standards start from Page 6 (index 5)
    for pno in range(5, len(doc)):
        text = doc[pno].get_text('text')
        if 'Students who demonstrate understanding can:' not in text:
            continue

        parts = text.split('Students who demonstrate understanding can:')
        for i in range(1, len(parts)):
            prev_part = parts[i - 1]
            curr_part = parts[i]

            prev_lines = [l.strip() for l in prev_part.split('\n') if l.strip()]
            header_line = prev_lines[-1] if prev_lines else ''

            curr_lines = [l.strip() for l in curr_part.split('\n') if l.strip()]
            first_line = curr_lines[0] if curr_lines else ''

            # Match standard code
            m_code = re.match(r'^((?:[0-9A-Za-z]+-)?[0-9A-Za-z]+-[0-9A-Za-z]+-[0-9]+)\.?\b(.*)', first_line)
            if not m_code:
                m_code_h = re.match(r'^((?:[0-9A-Za-z]+-)?[0-9A-Za-z]+-[0-9A-Za-z]+-[0-9]+)\.?\b(.*)', header_line)
                if m_code_h:
                    std_id = m_code_h.group(1).rstrip('.')
                    domain = m_code_h.group(2).strip()
                    remainder_first_line = first_line
                else:
                    continue
            else:
                std_id = m_code.group(1).rstrip('.')
                remainder_first_line = m_code.group(2).strip()
                m_domain = re.match(r'^(?:(?:[0-9A-Za-z]+-)?[0-9A-Za-z]+-[0-9A-Za-z]+-[0-9]+\.?\s*)?(.*)', header_line)
                domain = m_domain.group(1).strip() if m_domain else ''
                domain = re.sub(r'^\d+\s*', '', domain).strip()
                if not domain and len(prev_lines) >= 2:
                    domain = prev_lines[-2].strip()

            is_essential = False
            line_idx = 1
            if remainder_first_line.lower().startswith('essential'):
                is_essential = True
                remainder_first_line = remainder_first_line[9:].strip()
            elif len(curr_lines) > 1 and curr_lines[1].strip().lower() == 'essential':
                is_essential = True
                line_idx = 2

            desc_lines = []
            if remainder_first_line:
                desc_lines.append(remainder_first_line)

            sep_lines = []
            dci_lines = []
            ccc_lines = []

            current_section = 'desc'

            while line_idx < len(curr_lines):
                l = curr_lines[line_idx]

                # If multiple standards on page, don't overshoot into next standard's header
                if i < len(parts) - 1 and line_idx >= len(curr_lines) - 2:
                    if re.match(r'^((?:[0-9A-Za-z]+-)?[0-9A-Za-z]+-[0-9A-Za-z]+-[0-9]+)\b', l):
                        break

                if 'Science and Engineering Practices' in l:
                    current_section = 'sep'
                    line_idx += 1
                    continue
                elif 'Disciplinary Core Ideas' in l:
                    current_section = 'dci'
                    line_idx += 1
                    continue
                elif 'Crosscutting Concepts' in l:
                    current_section = 'ccc'
                    line_idx += 1
                    continue
                elif 'Note: Performance Expectations' in l or 'Connections to Nature of Science' in l:
                    if 'Note: Performance' in l:
                        break

                if l == str(pno + 1) or '2023 Indiana Academic Standards' in l or l == '●':
                    line_idx += 1
                    continue

                # Skip NGSS cross-reference / framework boilerplate that some files
                # (e.g. Integrated Chemistry and Physics, a few Earth & Space PEs)
                # place between the performance expectation and the SEP section.
                if (current_section == 'desc'
                        and (l.startswith('Reference: NGSS')
                             or 'performance expectation above was developed' in l)):
                    line_idx += 1
                    continue

                if current_section == 'desc':
                    desc_lines.append(l)
                elif current_section == 'sep':
                    sep_lines.append(l)
                elif current_section == 'dci':
                    dci_lines.append(l)
                elif current_section == 'ccc':
                    ccc_lines.append(l)

                line_idx += 1

            desc_full = ' '.join(desc_lines)
            desc_full = re.sub(r'\s+', ' ', desc_full).strip()

            clarification = None
            m_clar = re.search(r'\[Clarification Statement:\s*(.*?)\]', desc_full, re.IGNORECASE)
            if m_clar:
                clarification = m_clar.group(1).strip()
                desc_full = re.sub(r'\[Clarification Statement:\s*.*?\]', '', desc_full).strip()

            def clean_dimension(lines):
                if not lines:
                    return None
                cleaned = []
                for cl in lines:
                    cl_str = cl.strip()
                    if cl_str and cl_str != '●' and not cl_str.startswith('Note:'):
                        cleaned.append(cl_str)
                joined = ' '.join(cleaned)
                return re.sub(r'\s+', ' ', joined).strip() if joined else None

            standards.append({
                'id': std_id,
                'subject': 'Science',
                'grade_or_course': course_name,
                'grade_band': grade_band,
                'standard_type': 'Content Standard',
                'domain': domain,
                'learning_outcome': None,
                'title': domain,
                'description': desc_full,
                'is_essential': is_essential,
                'clarification_statement': clarification,
                'science_dimensions': {
                    'practices': clean_dimension(sep_lines),
                    'core_ideas': clean_dimension(dci_lines),
                    'crosscutting_concepts': clean_dimension(ccc_lines)
                },
                'source_file': rel_path,
                'page_number': pno + 1
            })

    return course_name, grade_band, standards


ZERO_WIDTH = re.compile('[​‌‍﻿]')


def _lines_with_boxes(page):
    """Every non-empty text line on the page as (x0, y0, text), in reading order.

    Column position matters in both of the layouts below: a code sits in the
    left cell of a table and its text in the right cell, and plain text
    extraction interleaves them in a way that cannot be paired reliably.
    """
    out = []
    for block in page.get_text('dict')['blocks']:
        if block['type'] != 0:
            continue
        for line in block['lines']:
            text = ''.join(span['text'] for span in line['spans'])
            text = ' '.join(ZERO_WIDTH.sub('', text).split())
            if text:
                out.append((round(line['bbox'][0], 1), round(line['bbox'][1], 1), text))
    return out


# --------------------------------------------------------------------------
# Mathematics, 2020 advanced courses
# --------------------------------------------------------------------------

M2020_FOOTER = re.compile(r'^Mathematics\b.*\bPage\s*\d+\s*-\s*\d{1,2}/\d{1,2}/\d{4}$')
M2020_COURSE_HEADER = re.compile(r'^MATHEMATICS:\s', re.IGNORECASE)
M2020_PROCESS = re.compile(r'^PS\.(\d+):\s*(.*)$')
# Three segments, e.g. C.LC.1, FM.MA.2, PS.DA.3. Deliberately does not match
# the two-segment process codes such as PS.1.
M2020_ID = re.compile(r'^([A-Z]{1,5}(?:\.[A-Z0-9]{1,5}){1,2}\.\d+)\.?$')


def _m2020_process_standards(doc, rel_path):
    """Process standards live in a two-column table: title left, text right."""
    rows = []
    for pno in range(len(doc)):
        for x0, y0, text in _lines_with_boxes(doc[pno]):
            if not M2020_FOOTER.match(text):
                rows.append((pno + 1, x0, y0, text))

    anchors = [r for r in rows if M2020_PROCESS.match(r[3])]
    if not anchors:
        return []

    left_x = min(r[1] for r in anchors)
    right_candidates = [r[1] for r in rows if r[1] > left_x + 60]
    if not right_candidates:
        return []
    split_x = (left_x + min(right_candidates)) / 2

    standards = []
    current = None
    for page, x0, _y0, text in rows:
        m = M2020_PROCESS.match(text)
        if m:
            current = {
                'id': f'PS.{m.group(1)}',
                'page': page,
                'title_parts': [m.group(2)] if m.group(2) else [],
                'desc_parts': [],
            }
            standards.append(current)
            continue
        if current is None:
            continue
        if x0 >= split_x:
            current['desc_parts'].append(text)
        elif not current['desc_parts'] and not M2020_COURSE_HEADER.match(text):
            # Still inside the left cell, so this is the rest of the title.
            current['title_parts'].append(text)

    records = []
    for s in standards:
        title = ' '.join(s['title_parts']).strip()
        desc = ' '.join(s['desc_parts']).strip()
        records.append({
            'id': s['id'],
            'subject': 'Mathematics',
            'standard_type': 'Process Standard',
            'domain': 'Mathematics Process Standards',
            'learning_outcome': None,
            'title': title or None,
            'description': (f"{title.rstrip('.')}. {desc}".strip()
                            if title else desc),
            'is_essential': False,
            'clarification_statement': None,
            'science_dimensions': None,
            'indiana_specific': None,
            'source_file': rel_path,
            'page_number': s['page'],
        })
    return records


def _m2020_content_standards(doc, rel_path):
    """Content standards are a single column: the code alone on its own line."""
    stream = []
    for pno in range(len(doc)):
        for _x0, _y0, text in _lines_with_boxes(doc[pno]):
            if M2020_FOOTER.match(text) or M2020_COURSE_HEADER.match(text):
                continue
            if M2020_PROCESS.match(text):
                continue
            stream.append((text, pno + 1))

    positions = [i for i, (text, _p) in enumerate(stream) if M2020_ID.match(text)]
    if not positions:
        return []

    def prefix_of(code):
        return code.rsplit('.', 1)[0]

    # The heading for the first group sits just above the first code.
    domain = stream[positions[0] - 1][0] if positions[0] > 0 else 'General'

    records = []
    for n, pos in enumerate(positions):
        code = M2020_ID.match(stream[pos][0]).group(1)
        end = positions[n + 1] if n + 1 < len(positions) else len(stream)
        body = [stream[i][0] for i in range(pos + 1, end)]

        next_domain = None
        if n + 1 < len(positions):
            next_code = M2020_ID.match(stream[positions[n + 1]][0]).group(1)
            # A new domain heading appears only where the code prefix changes,
            # so a stray short line inside a description is not mistaken for one.
            if prefix_of(next_code) != prefix_of(code) and body:
                next_domain = body.pop()

        records.append({
            'id': code,
            'subject': 'Mathematics',
            'standard_type': 'Content Standard',
            'domain': domain,
            'learning_outcome': None,
            'title': None,
            'description': ' '.join(body).strip(),
            'is_essential': False,  # the 2020 documents carry no (E) markers
            'clarification_statement': None,
            'science_dimensions': None,
            'indiana_specific': None,
            'source_file': rel_path,
            'page_number': stream[pos][1],
        })
        if next_domain:
            domain = next_domain
    return records


def parse_math_2020_pdf(pdf_path, base_dir):
    """Parses a 2020 Indiana advanced mathematics standards PDF."""
    doc = fitz.open(pdf_path)
    rel_path = os.path.relpath(pdf_path, base_dir).replace('\\', '/')
    standards = (_m2020_process_standards(doc, rel_path)
                 + _m2020_content_standards(doc, rel_path))
    doc.close()
    # The course name comes from the manifest; these are placeholders.
    return None, 'High School', standards


# --------------------------------------------------------------------------
# Science, 2022 elective courses
# --------------------------------------------------------------------------

# One page in the Anatomy and Physiology document omits the trailing colon,
# so the constant stops short of it and matching is done with startswith.
S2022_ANCHOR = 'Students who demonstrate understanding can'
S2022_CODE = re.compile(r'^((?:HS|MS|\d)-[A-Za-z0-9]+-\d+(?:\.\d+)?)\.?(\*?)\.?$')
S2022_GROUP = re.compile(r'^Standard\s+[A-Za-z0-9]+:\s*(.+)$')
S2022_GROUP_CODE = re.compile(r'^(?:HS|MS|\d)-[A-Za-z0-9]+-\d+\s+(.+)$')
S2022_CLARIFY = re.compile(r'\[Clarification Statement:\s*(.*?)\]\s*', re.DOTALL)
S2022_DIMENSIONS = {
    'Science and Engineering Practices': 'practices',
    'Disciplinary Core Ideas': 'core_ideas',
    'Crosscutting Concepts': 'crosscutting_concepts',
}


def _s2022_dimensions(page_lines, spans):
    """Collect the three dimensions printed beneath one standard group.

    `spans` are the (page, y_from, y_to) regions that belong to this group.
    Practices sit in the left column, core ideas and crosscutting concepts in
    the right one, each under its own heading, and a group can run on to the
    pages that follow it.
    """
    collected = {'practices': [], 'core_ideas': [], 'crosscutting_concepts': []}
    for pno, y_from, y_to, mid_x in spans:
        lines = [l for l in page_lines.get(pno, []) if y_from <= l[1] < y_to]
        headings = [(x0, y0, S2022_DIMENSIONS[t])
                    for x0, y0, t in lines if t in S2022_DIMENSIONS]
        for hx, hy, key in headings:
            right = hx >= mid_x
            below = [h[1] for h in headings
                     if h[1] > hy and (h[0] >= mid_x) == right]
            stop = min(below) if below else y_to
            for lx, ly, text in lines:
                if not (hy < ly < stop) or (lx >= mid_x) != right:
                    continue
                if text in S2022_DIMENSIONS or text in ('●', '•'):
                    continue
                collected[key].append(text)
    return {k: (' '.join(v).strip() or None) for k, v in collected.items()}


def parse_science_2022_pdf(pdf_path, base_dir):
    """Parses a 2022 Indiana elective science standards PDF.

    Codes sit in the left cell of a table and their performance expectations in
    the right cell, so the two are paired by vertical position. A page can hold
    more than one standard group, and a group can continue on to later pages.
    """
    doc = fitz.open(pdf_path)
    rel_path = os.path.relpath(pdf_path, base_dir).replace('\\', '/')

    page_lines = {}
    mid_x = {}
    anchors = []
    for pno in range(len(doc)):
        page_lines[pno] = [l for l in _lines_with_boxes(doc[pno]) if 60 < l[1] < 725]
        mid_x[pno] = doc[pno].rect.width / 2
        for _x, y, text in page_lines[pno]:
            # One document drops the trailing colon, so match on the phrase.
            if text.startswith(S2022_ANCHOR):
                anchors.append((pno, y))
    anchors.sort()

    standards = []
    for n, (pno, anchor_y) in enumerate(anchors):
        lines = page_lines[pno]
        nxt = anchors[n + 1] if n + 1 < len(anchors) else (len(doc), 0.0)

        above = [(y, text) for _x, y, text in lines if y < anchor_y]
        heading = max(above)[1] if above else ''
        m_group = S2022_GROUP.match(heading) or S2022_GROUP_CODE.match(heading)
        domain = m_group.group(1).strip() if m_group else heading.strip()

        # The expectations table ends where the dimensions begin.
        dim_tops = [y for _x, y, text in lines
                    if text in S2022_DIMENSIONS and y > anchor_y]
        table_bottom = min(dim_tops) if dim_tops else 10_000.0
        region = [(x, y, t) for x, y, t in lines if anchor_y < y < table_bottom]

        codes = sorted((r for r in region if S2022_CODE.match(r[2])),
                       key=lambda r: r[1])
        if not codes:
            continue
        code_x = min(c[0] for c in codes)
        body = [(y, t) for x, y, t in region
                if x > code_x + 40 and not S2022_CODE.match(t)]

        # Dimensions run from the end of this table to the next group, which
        # may be further down this page or several pages later.
        if nxt[0] == pno:
            spans = [(pno, table_bottom, nxt[1], mid_x[pno])]
        else:
            spans = [(pno, table_bottom, 10_000.0, mid_x[pno])]
            for p in range(pno + 1, min(nxt[0], len(doc))):
                spans.append((p, 0.0, nxt[1] if p == nxt[0] else 10_000.0, mid_x[p]))
        dimensions = _s2022_dimensions(page_lines, spans)

        for i, (_cx, cy, ctext) in enumerate(codes):
            m = S2022_CODE.match(ctext)
            code, star = m.group(1), m.group(2)
            top = cy - 4
            bottom = (codes[i + 1][1] - 4) if i + 1 < len(codes) else 10_000.0
            text = ' '.join(t for y, t in sorted(body) if top <= y < bottom).strip()

            clarification = None
            m_clar = S2022_CLARIFY.search(text)
            if m_clar:
                clarification = ' '.join(m_clar.group(1).split())
                text = S2022_CLARIFY.sub('', text).strip()

            standards.append({
                'id': code,
                'subject': 'Science',
                'standard_type': 'Content Standard',
                'domain': domain,
                'learning_outcome': None,
                'title': domain or None,
                'description': text,
                'is_essential': False,  # no essential markers in these documents
                'clarification_statement': clarification,
                'science_dimensions': dimensions,
                'indiana_specific': bool(star),
                'source_file': rel_path,
                'page_number': pno + 1,
            })

    doc.close()
    return None, 'High School', standards


PARSERS = {
    'math_2023': parse_math_pdf,
    'science_2023': parse_science_pdf,
    'math_2020': parse_math_2020_pdf,
    'science_2022': parse_science_2022_pdf,
}


def load_manifest(workspace_dir):
    path = os.path.join(workspace_dir, 'course_manifest.json')
    if not os.path.exists(path):
        raise SystemExit(
            'course_manifest.json not found. Run build_manifest.py first.')
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def stamp_record(raw, doc):
    """Rebuild one parsed standard with its provenance attached.

    The parsed record knows what the text said. Only the manifest knows which
    published version it came from, which URL, and which exact file. Without
    that, a citation cannot be checked against a source.
    """
    code = raw['id']
    return {
        # Composite key. A bare code is not unique: PS.1 appears once per
        # mathematics course, and several science codes recur across courses.
        'standard_key': '|'.join([
            doc['jurisdiction'], doc['standards_version'],
            doc['subject'], doc['course'], code,
        ]),
        'printed_code': code,
        'id': code,  # kept so existing callers do not break
        'jurisdiction': doc['jurisdiction'],
        'standards_version': doc['standards_version'],
        'subject': doc['subject'],
        'grade_or_course': doc['course'],
        'grade_band': doc['grade_band'],
        'standard_type': raw['standard_type'],
        'domain': raw['domain'],
        'learning_outcome': raw['learning_outcome'],
        'title': raw['title'],
        'description': raw['description'],
        'is_essential': raw['is_essential'],
        'clarification_statement': raw['clarification_statement'],
        'science_dimensions': raw['science_dimensions'],
        # Marked with an asterisk in the 2022 electives; None where the
        # document does not make the distinction at all.
        'indiana_specific': raw.get('indiana_specific'),
        'source_file': raw['source_file'],
        'page_number': raw['page_number'],
        'source_url': doc['source_url'],
        'document_sha256': doc['sha256'],
        'extraction_review_status': doc['extraction_review_status'],
    }


def build_index(workspace_dir):
    """Parse every document the manifest marks extractable, then write the index."""
    manifest = load_manifest(workspace_dir)

    all_standards = []
    hierarchy = {'Mathematics': {}, 'Science': {}}
    course_stats = {'Mathematics': {}, 'Science': {}}
    grade_band_stats = {}
    subject_stats = {
        'Mathematics': {'total': 0, 'essential': 0,
                        'process_standards': 0, 'content_standards': 0},
        'Science': {'total': 0, 'essential': 0, 'content_standards': 0},
    }

    extractable = [d for d in manifest['documents'] if d['parser']]
    pending = [d for d in manifest['documents'] if not d['parser']]
    print(f"Manifest lists {len(manifest['documents'])} documents: "
          f"{len(extractable)} extractable, {len(pending)} awaiting a parser.\n")

    for doc in extractable:
        parser = PARSERS.get(doc['parser'])
        if parser is None:
            raise SystemExit(f"No parser named {doc['parser']!r} "
                             f"for {doc['course_key']}")

        pdf_path = os.path.join(workspace_dir, doc['local_path'].replace('/', os.sep))
        if not os.path.exists(pdf_path):
            raise SystemExit(f"Missing PDF for {doc['course_key']}: {pdf_path}")

        # If the file on disk is not the file the manifest measured, every
        # page number and hash below would be a lie. Stop instead.
        actual = sha256_of(pdf_path)
        if actual != doc['sha256']:
            raise SystemExit(
                f"Hash mismatch for {doc['course_key']}\n"
                f"  manifest: {doc['sha256']}\n"
                f"  on disk:  {actual}\n"
                f"Re-run build_manifest.py if the document was updated on purpose.")

        guessed_course, guessed_band, raw_records = parser(pdf_path, workspace_dir)
        # The newer parsers return None and leave naming to the manifest; the
        # 2023 ones still guess from the file name, so a disagreement is worth
        # seeing.
        if guessed_course is not None and guessed_course != doc['course']:
            print(f"  note: parser read the course as {guessed_course!r}; "
                  f"manifest value {doc['course']!r} wins.")

        records = [stamp_record(r, doc) for r in raw_records]
        all_standards.extend(records)
        print(f"  [{doc['subject'][:4]}] {doc['course']:32} "
              f"{doc['standards_version']}  -> {len(records):3} standards")

        subject = doc['subject']
        course = doc['course']
        node = hierarchy[subject].setdefault(course, {
            'course_key': doc['course_key'],
            'standards_version': doc['standards_version'],
            'grade_band': doc['grade_band'],
            'source_file': doc['local_path'],
            'source_url': doc['source_url'],
            'document_sha256': doc['sha256'],
            'domains': {},
        })

        for s in records:
            domain = node['domains'].setdefault(s['domain'], {
                'learning_outcome': s['learning_outcome'],
                'standards': [],
            })
            domain['standards'].append(s['standard_key'])

            subject_stats[subject]['total'] += 1
            if s['is_essential']:
                subject_stats[subject]['essential'] += 1
            if subject == 'Mathematics':
                if s['standard_type'] == 'Process Standard':
                    subject_stats[subject]['process_standards'] += 1
                else:
                    subject_stats[subject]['content_standards'] += 1
            else:
                subject_stats[subject]['content_standards'] += 1

            band = s['grade_band']
            grade_band_stats[band] = grade_band_stats.get(band, 0) + 1
            course_stats[subject][course] = course_stats[subject].get(course, 0) + 1

    # The composite key should be unique, and where it is not, the cause is a
    # repeated code in the published document rather than a parsing fault. That
    # is a real ambiguity a teacher would also meet, so record it on the records
    # instead of inventing a code the document does not print. Adding the domain
    # must then separate them; if even that fails, something is genuinely wrong.
    key_counts = collections.Counter(s['standard_key'] for s in all_standards)
    ambiguous = sorted(k for k, n in key_counts.items() if n > 1)
    for s in all_standards:
        s['standard_key_ambiguous'] = key_counts[s['standard_key']] > 1

    qualified = collections.Counter(
        (s['standard_key'], s['domain']) for s in all_standards)
    unresolved = sorted(k for k, n in qualified.items() if n > 1)
    if unresolved:
        raise SystemExit(
            'Repeated standard_key that the domain does not separate:\n  ' +
            '\n  '.join(f'{k[0]}  [{k[1]}]' for k in unresolved[:20]))

    total = len(all_standards)
    essential = sum(1 for s in all_standards if s['is_essential'])
    versions = sorted({s['standards_version'] for s in all_standards})

    output_data = {
        'metadata': {
            'title': 'Indiana Academic Standards Index (Mathematics & Science)',
            'description': (
                'Standards extracted from the official IDOE documents listed in '
                'course_manifest.json. Each record carries its own published '
                'version; the index as a whole is not a single version.'),
            'generated_at': datetime.now().isoformat(),
            'canonical_source': 'standards_extract/standards_index.json',
            'manifest_version': manifest['manifest_version'],
            'jurisdiction': manifest['jurisdiction'],
            'scope': manifest['scope'],
            'standards_versions_present': versions,
            'ambiguous_standard_keys': ambiguous,
            'coverage': {
                'documents_in_manifest': len(manifest['documents']),
                'documents_extracted': len(extractable),
                'documents_pending_parser': len(pending),
                'courses_pending': [
                    {'course_key': d['course_key'], 'reason': d['notes']}
                    for d in pending
                ],
            },
            'total_files_indexed': len(extractable),
            'total_standards': total,
            'essential_standards_count': essential,
            'non_essential_standards_count': total - essential,
            'subjects': ['Mathematics', 'Science'],
            'summary_statistics': {
                'by_subject': subject_stats,
                'by_grade_band': grade_band_stats,
                'by_grade_or_course': course_stats,
            },
        },
        'hierarchy': hierarchy,
        'standards': all_standards,
    }

    out_file = os.path.join(workspace_dir, 'standards_index.json')
    with open(out_file, 'w', encoding='utf-8') as fh:
        json.dump(output_data, fh, indent=2, ensure_ascii=False)

    # The app reads its own copy. Write identical bytes so that a hash
    # comparison is enough to prove the two have not drifted apart.
    app_copy = os.path.join(os.path.dirname(workspace_dir), 'app', 'data',
                            'standards_index.json')
    if os.path.isdir(os.path.dirname(app_copy)):
        shutil.copyfile(out_file, app_copy)
        print(f'\nCopied build artifact to {app_copy}')

    print(f"\n[SUCCESS] Indexed {total} standards into '{out_file}'")
    print(f"  - Mathematics: {subject_stats['Mathematics']['total']} "
          f"(Essential: {subject_stats['Mathematics']['essential']})")
    print(f"  - Science:     {subject_stats['Science']['total']} "
          f"(Essential: {subject_stats['Science']['essential']})")
    print(f"  - Versions present: {', '.join(versions)}")
    if pending:
        print(f"\n  {len(pending)} courses listed but not extracted:")
        for d in pending:
            print(f"    - {d['course_key']}")
    return out_file


if __name__ == '__main__':
    workspace = os.path.abspath(os.path.dirname(__file__))
    build_index(workspace)
