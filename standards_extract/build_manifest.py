#!/usr/bin/env python3
"""Build course_manifest.json: one row per official standards document.

The manifest is the source of truth for which courses this project covers,
which published version each one is, and where its PDF came from. The indexer
reads it instead of guessing course names from file names, so every standard
record can be traced back to a specific document and a specific hash.

Curated fields (course, version, source_url, served) are edited by hand.
Measured fields (sha256, bytes, page_count, downloaded_at) are computed here.
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import fitz  # PyMuPDF

sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.abspath(os.path.dirname(__file__))
JURISDICTION = 'IN'
MANIFEST_VERSION = '1.0.0'

# Publication years verified against the IDOE standards pages on 2026-09-08.
# Mathematics: https://www.in.gov/doe/students/indiana-academic-standards/mathematics/
# Science:     https://www.in.gov/doe/students/indiana-academic-standards/science-and-computer-science/
VERSION_SOURCE = 'IDOE standards pages, checked 2026-09-08'

MEDIA = 'https://media.doe.in.gov/standards/'
FILES = 'https://www.in.gov/doe/files/'
STUFILES = 'https://www.in.gov/doe/students/files/'

# parser=None means no parser branch handles this layout yet. served=False
# means the course is extracted but held back for the reason in its note.
DOCUMENTS = [
    # --- Mathematics, 2023 prioritised standards -------------------------
    ('Mathematics', 'Grade 5', 'Elementary (Grade 5)', '2023',
     '2023 Indiana Academic Standards: Grade 5 Mathematics',
     MEDIA + 'indiana-academic-standards-grade-5-mathematics.pdf',
     'standards/maths/indiana-academic-standards-grade-5-mathematics.pdf',
     'math_2023', True, None),
    ('Mathematics', 'Grade 6', 'Middle School (Grades 6-8)', '2023',
     '2023 Indiana Academic Standards: Grade 6 Mathematics',
     MEDIA + 'indiana-academic-standards-grade-6-mathematics.pdf',
     'standards/maths/indiana-academic-standards-grade-6-mathematics.pdf',
     'math_2023', True, None),
    ('Mathematics', 'Grade 7', 'Middle School (Grades 6-8)', '2023',
     '2023 Indiana Academic Standards: Grade 7 Mathematics',
     MEDIA + 'indiana-academic-standards-grade-7-mathematics.pdf',
     'standards/maths/indiana-academic-standards-grade-7-mathematics.pdf',
     'math_2023', True, None),
    ('Mathematics', 'Grade 8', 'Middle School (Grades 6-8)', '2023',
     '2023 Indiana Academic Standards: Grade 8 Mathematics',
     MEDIA + 'indiana-academic-standards-grade-8-mathematics.pdf',
     'standards/maths/indiana-academic-standards-grade-8-mathematics.pdf',
     'math_2023', True, None),
    ('Mathematics', 'Algebra I', 'High School', '2023',
     '2023 Indiana Academic Standards: Algebra I',
     FILES + 'Indiana-Academic-Standards-Algebra-I-.pdf',
     'standards/maths/Indiana-Academic-Standards-Algebra-I-.pdf',
     'math_2023', True, None),
    ('Mathematics', 'Algebra II', 'High School', '2023',
     '2023 Indiana Academic Standards: Algebra II',
     FILES + 'Indiana-Academic-Standards-Algebra-II-.pdf',
     'standards/maths/Indiana-Academic-Standards-Algebra-II-.pdf',
     'math_2023', True, None),
    ('Mathematics', 'Analytical Algebra II', 'High School', '2023',
     '2023 Indiana Academic Standards: Analytical Algebra II',
     FILES + 'Indiana-Academic-Standards-Analytical-Algebra-II.pdf',
     'standards/maths/Indiana-Academic-Standards-Analytical-Algebra-II.pdf',
     'math_2023', True, None),
    ('Mathematics', 'Geometry', 'High School', '2023',
     '2023 Indiana Academic Standards: Geometry',
     MEDIA + 'indiana-academic-standards-geometry.pdf',
     'standards/maths/indiana-academic-standards-geometry.pdf',
     'math_2023', True, None),

    # --- Mathematics, 2020 advanced courses ------------------------------
    ('Mathematics', 'Calculus', 'High School', '2020',
     'Indiana Academic Standards Mathematics: Calculus',
     STUFILES + 'Calculus%20Standards%20-%20UPDATED%20March%202020.pdf',
     'standards/maths/Calculus Standards - UPDATED March 2020.pdf',
     'math_2020', True,
     '2020 layout: process standards in a two-column table, a running page '
     'footer, no essential (E) markers and no learning outcome statements. '
     'Parsed by the math_2020 branch.'),
    ('Mathematics', 'Finite Mathematics', 'High School', '2020',
     'Indiana Academic Standards Mathematics: Finite',
     STUFILES + 'Finite%20Standards%20-%20UPDATED%20March%202020.pdf',
     'standards/maths/Finite Standards - UPDATED March 2020.pdf',
     'math_2020', True, 'Same 2020 layout as Calculus.'),
    ('Mathematics', 'Pre-Calculus: Algebra', 'High School', '2020',
     'Indiana Academic Standards Mathematics: Precalculus: Algebra',
     STUFILES + 'Precalculus%20Standards%20UPDATED%20March%202020.pdf',
     'standards/maths/Precalculus Standards UPDATED March 2020.pdf',
     'math_2020', True, 'Same 2020 layout as Calculus.'),
    ('Mathematics', 'Pre-Calculus: Trigonometry', 'High School', '2020',
     'Indiana Academic Standards Mathematics: PreCalculus: Trigonometry',
     STUFILES + 'Trigonometry%20Standards%20UPDATED%20March%202020.pdf',
     'standards/maths/Trigonometry Standards UPDATED March 2020.pdf',
     'math_2020', True, 'Same 2020 layout as Calculus.'),
    ('Mathematics', 'Probability and Statistics', 'High School', '2020',
     'Indiana Academic Standards Mathematics: Probability and Statistics',
     STUFILES + 'Probability%20and%20Statistics%20Standards%20UPDATED%20March%202020.pdf',
     'standards/maths/Probability and Statistics Standards UPDATED March 2020.pdf',
     'math_2020', True,
     'Same 2020 layout as Calculus. The running title on the content page of '
     'this PDF reads "MATHEMATICS: Finite", which is an error in the published '
     'document; the course name here comes from the IDOE listing.'),
    ('Mathematics', 'Quantitative Reasoning', 'High School', '2020',
     'Indiana Academic Standards Mathematics: Quantitative Reasoning',
     STUFILES + 'Quantitative%20Reasoning%20Standards%20-%20UPDATED%20March%202020.pdf',
     'standards/maths/Quantitative Reasoning Standards - UPDATED March 2020.pdf',
     'math_2020', False,
     'Extracted, but not served: the published document prints QR.P.1 to QR.P.4 '
     'twice, once under "Ratio and Proportional Reasoning" and again under '
     '"Probabilistic Reasoning to Assess Risk". A bare code therefore does not '
     'identify one standard in this course. Serve it once that is resolved.'),

    # --- Science, 2023 prioritised standards -----------------------------
    ('Science', 'Grade 5', 'Elementary (Grade 5)', '2023',
     '2023 Indiana Academic Standards: Grade 5 Science',
     MEDIA + 'indiana-academic-standards-grade-5-science.pdf',
     'standards/science/indiana-academic-standards-grade-5-science.pdf',
     'science_2023', True, None),
    ('Science', 'Grade 6', 'Middle School (Grades 6-8)', '2023',
     '2023 Indiana Academic Standards: Grade 6 Science',
     MEDIA + 'indiana-academic-standards-grade-6-science.pdf',
     'standards/science/indiana-academic-standards-grade-6-science.pdf',
     'science_2023', True, None),
    ('Science', 'Grade 7', 'Middle School (Grades 6-8)', '2023',
     '2023 Indiana Academic Standards: Grade 7 Science',
     MEDIA + 'indiana-academic-standards-grade-7-science.pdf',
     'standards/science/indiana-academic-standards-grade-7-science.pdf',
     'science_2023', True, None),
    ('Science', 'Grade 8', 'Middle School (Grades 6-8)', '2023',
     '2023 Indiana Academic Standards: Grade 8 Science',
     MEDIA + 'indiana-academic-standards-grade-8-science.pdf',
     'standards/science/indiana-academic-standards-grade-8-science.pdf',
     'science_2023', True, None),
    ('Science', 'Biology', 'High School', '2023',
     '2023 Indiana Academic Standards: Biology',
     MEDIA + 'indiana-academic-standards-biology.pdf',
     'standards/science/indiana-academic-standards-biology.pdf',
     'science_2023', True, None),
    ('Science', 'Chemistry', 'High School', '2023',
     '2023 Indiana Academic Standards: Chemistry',
     MEDIA + 'indiana-academic-standards-chemistry.pdf',
     'standards/science/indiana-academic-standards-chemistry.pdf',
     'science_2023', True, None),
    ('Science', 'Earth and Space Science', 'High School', '2023',
     '2023 Indiana Academic Standards: Earth and Space Science',
     MEDIA + 'indiana-academic-standards-earth-and-space-science.pdf',
     'standards/science/indiana-academic-standards-earth-and-space-science.pdf',
     'science_2023', True, None),
    ('Science', 'Integrated Chemistry and Physics', 'High School', '2023',
     '2023 Indiana Academic Standards: Integrated Chemistry and Physics',
     MEDIA + 'indiana-academic-standards-integrated-chemistry-and-physics.pdf',
     'standards/science/indiana-academic-standards-integrated-chemistry-and-physics.pdf',
     'science_2023', True, None),
    ('Science', 'Physics I', 'High School', '2023',
     '2023 Indiana Academic Standards: Physics I',
     MEDIA + 'indiana-academic-standards-physics-i.pdf',
     'standards/science/indiana-academic-standards-physics-i.pdf',
     'science_2023', True, None),

    # --- Science, 2022 elective courses ----------------------------------
    ('Science', 'Physics II', 'High School', '2022',
     'Indiana Academic Standards Science: Physics II',
     'https://media.doe.in.gov/news/physics-2-standards.pdf',
     'standards/science/physics-2-standards.pdf',
     'science_2022', True,
     'Elective course published 2022. Codes and expectations sit in a two-column '
     'table, paired by vertical position. Parsed by the science_2022 branch.'),
    ('Science', 'Anatomy and Physiology', 'High School', '2022',
     'Indiana Academic Standards Science: Anatomy and Physiology',
     'https://media.doe.in.gov/news/ap-standards.pdf',
     'standards/science/ap-standards.pdf',
     'science_2022', True,
     'Elective course published 2022. One page omits the colon after "Students '
     'who demonstrate understanding can", which the parser tolerates.'),
    ('Science', 'Environmental Science', 'High School', '2022',
     'Indiana Academic Standards Science: Environmental Science',
     FILES + 'env-sci-standards_update_2_27_2024.pdf',
     'standards/science/env-sci-standards_update_2_27_2024.pdf',
     'science_2022', False,
     'Extracted, but not served: IDOE lists this as 2022 while the PDF was '
     'modified 2024-02-27 and the file name says so. Confirm which text and '
     'which year are current, then serve it.'),
]


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def build():
    documents = []
    listed_paths = set()
    missing = []

    for (subject, course, band, version, title, url, rel, parser, served, notes) in DOCUMENTS:
        abs_path = os.path.join(BASE, rel.replace('/', os.sep))
        listed_paths.add(os.path.normcase(abs_path))
        if not os.path.exists(abs_path):
            missing.append(rel)
            continue

        doc = fitz.open(abs_path)
        meta = doc.metadata or {}
        pdf_title = meta.get('title') or None
        pdf_moddate = meta.get('modDate') or None
        page_count = len(doc)
        doc.close()

        stat = os.stat(abs_path)
        documents.append({
            'course_key': f'{JURISDICTION}|{version}|{subject}|{course}',
            'jurisdiction': JURISDICTION,
            'subject': subject,
            'course': course,
            'grade_band': band,
            'standards_version': version,
            'version_source': VERSION_SOURCE,
            'document_title': title,
            'pdf_metadata_title': pdf_title,
            'pdf_mod_date': pdf_moddate,
            'source_url': url,
            'local_path': rel,
            'sha256': sha256_of(abs_path),
            'bytes': stat.st_size,
            'page_count': page_count,
            'downloaded_at': datetime.fromtimestamp(
                stat.st_mtime, timezone.utc).date().isoformat(),
            'parser': parser,
            'extraction_status': 'extracted' if parser else 'not_extracted',
            'extraction_review_status': 'unreviewed',
            'served': served,
            'notes': notes,
        })

    if missing:
        raise SystemExit('Listed in the manifest but missing on disk:\n  ' +
                         '\n  '.join(missing))

    # A PDF sitting in the tree that nobody listed is drift; say so loudly.
    on_disk = []
    for sub in ('maths', 'science'):
        folder = os.path.join(BASE, 'standards', sub)
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith('.pdf'):
                on_disk.append(os.path.join(folder, name))
    unlisted = [p for p in on_disk if os.path.normcase(p) not in listed_paths]

    by_version = {}
    for d in documents:
        v = d['standards_version']
        by_version[v] = by_version.get(v, 0) + 1

    manifest = {
        'manifest_version': MANIFEST_VERSION,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'jurisdiction': JURISDICTION,
        'scope': 'Indiana grades 5-12 mathematics and science',
        'note': ('Publication year is recorded per document, not once for the '
                 'whole index. Documents with parser=null are listed for '
                 'coverage but are not extracted and are not served.'),
        'counts': {
            'documents': len(documents),
            'served': sum(1 for d in documents if d['served']),
            'extracted': sum(1 for d in documents
                             if d['extraction_status'] == 'extracted'),
            'not_extracted': sum(1 for d in documents
                                 if d['extraction_status'] == 'not_extracted'),
            'by_version': by_version,
        },
        'documents': documents,
    }

    out = os.path.join(BASE, 'course_manifest.json')
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)

    c = manifest['counts']
    print(f'Wrote {out}')
    print(f"  documents: {c['documents']}   served: {c['served']}   "
          f"extracted: {c['extracted']}   pending: {c['not_extracted']}")
    print(f"  by version: {c['by_version']}")
    if unlisted:
        print('\nWARNING: PDFs on disk that the manifest does not list:')
        for p in unlisted:
            print('  ', os.path.relpath(p, BASE))
    return out


if __name__ == '__main__':
    build()
