# Accessibilizer

Accessibilizer turns visually readable source PDFs into documents whose content and structure can be meaningfully consumed with assistive technology.

## Language

**Accessible PDF**:
A PDF that passes veraPDF's PDF/UA-1 validation and Accessibilizer's semantic checks, including meaningful structure, reading order, text, mathematical content, descriptions of Informative Figures, source-region coverage, and Review Record consistency. An output with unresolved failures or Conversion Warnings is not an Accessible PDF.
_Avoid_: OCRed PDF, searchable PDF, screen-reader-friendly PDF

**Source PDF**:
The PDF supplied to Accessibilizer for conversion, whether it contains page images, unreliable embedded text, or both.
_Avoid_: Input scan, original

**Supported Source PDF**:
An English-language, static STEM lecture note or instructional document containing printed or handwritten prose, equations, diagrams, and tables. Encrypted, digitally signed, scripted, form-based, embedded-media, or otherwise interactive PDFs are not Supported Source PDFs; other static document classes are experimental and require an unsupported-input Conversion Warning.
_Avoid_: Any scanned PDF, arbitrary PDF

**Visual Layer**:
The preserved visible appearance of each Source PDF page. Accessibilization does not redraw or editorially improve this representation.
_Avoid_: Background image, original layer

**Semantic Layer**:
The ordered document structure, text, mathematical alternatives, and figure descriptions exposed to assistive technology independently of the Visual Layer.
_Avoid_: OCR layer, invisible text

**Formula**:
A mathematical expression recognized as a semantic unit rather than a sequence of OCR characters. Each Formula has a normalized mathematical representation and a Spoken Math Alternative.
_Avoid_: Equation text, math OCR string

**Spoken Math Alternative**:
Concise mathematical English that communicates a Formula to a screen-reader user, such as “I equals Q divided by delta t.”
_Avoid_: Raw LaTeX, character transcription, equation alt tag

**Logical Reading Order**:
The single instructional sequence in which Semantic Layer content is presented to assistive technology, based on authorial meaning rather than page coordinates. Multiple plausible sequences constitute a Conversion Warning.
_Avoid_: Visual order, OCR order, coordinate order

**Document Structure**:
The title, language, heading hierarchy, bookmarks, and Logical Reading Order that make the document navigable as a coherent whole. Ambiguous inferred structure produces a Conversion Warning.
_Avoid_: Metadata, outline, table of contents

**Source Fidelity**:
The requirement that the Semantic Layer communicate what the Source PDF actually says, including apparent author mistakes, rather than silently correcting or improving its content. A suspected source error is preserved and receives a Conversion Warning.
_Avoid_: Automatic correction, editorial cleanup

**Meaningful Visual Cue**:
Color, boxing, underlining, arrows, or spatial grouping that conveys an instructional role such as a heading, final result, derivation, or association. Its meaning is represented in the Semantic Layer without narrating purely decorative styling.
_Avoid_: Formatting, color description, decoration

**Informative Figure**:
A visual whose meaning is not fully available from surrounding content. A simple Informative Figure has a concise Figure Alternative; a complex one also has a Detailed Figure Description.
_Avoid_: Image, diagram needing an alt tag

**Figure Alternative**:
A concise identification and summary of an Informative Figure for assistive technology.
_Avoid_: Caption, filename, alt tag

**Detailed Figure Description**:
An extended explanation of a complex Informative Figure's components, relationships, directions, labels, and instructional purpose.
_Avoid_: Long alt text, caption

**Semantic Table**:
A table represented with its caption, row and column headers, cells, and header relationships intact. Uncertain boundaries, merged cells, or ambiguous headers produce a Conversion Warning.
_Avoid_: Table image, flattened table text

**Decorative Content**:
Visual material that adds no distinct instructional meaning and is omitted from the Semantic Layer.
_Avoid_: Decorative figure, empty alt text

**Conversion Warning**:
A specific unresolved concern, supported by failed verification, disagreement, or ambiguity, about the correctness or completeness of a generated Semantic Layer. It is resolved only when a Reviewer explicitly corrects it, accepts the candidate as accurate, or marks it inapplicable with a reason; model self-confidence alone does not resolve a concern.
_Avoid_: Error, validation failure

**Review-Required PDF**:
A generated PDF with one or more Conversion Warnings that must be manually reviewed before it can be treated as an Accessible PDF.
_Avoid_: Accessible PDF, failed PDF

**Review Record**:
An editable, durable account of recognized content, semantic alternatives, Logical Reading Order, Conversion Warnings, original candidates, resolution history, reviewer corrections, conversion provenance, and stable references to source regions. It can be finalized again without repeating recognition or model calls, but not while warnings remain unresolved.
_Avoid_: OCR output, log file, cache

**Conversion Bundle**:
The protected directory containing an immutable, hash-verified copy of the Source PDF; its PDF output, Review Record, review report, and source-region crops; and its Conversion Provenance and validation reports. Generated artifacts may be refreshed, but reviewer edits are preserved unless replacement is explicitly requested.
_Avoid_: Output folder, result files, cache directory

**Review Report**:
The WCAG 2.2 AA-conformant HTML presentation of a Review Record, pairing source context with generated interpretations, warnings, and resolutions for keyboard and assistive-technology users.
_Avoid_: Debug report, OCR preview, dashboard

**Conversion Provenance**:
The auditable identity of the Source PDF, Accessibilizer release, recognition models, LLM endpoint and exact model, prompt and schema versions, rendering settings, timestamps, and reviewer changes that produced a Review Record. It excludes credentials and hidden model reasoning.
_Avoid_: Logs, request dump, metadata

**Reviewer**:
A technically comfortable person with enough subject-matter knowledge to resolve ambiguous STEM content, but no required knowledge of PDF internals or accessibility tagging. Warning resolutions carry the Reviewer's configured non-secret identifier.
_Avoid_: PDF expert, accessibility engineer, proofreader
