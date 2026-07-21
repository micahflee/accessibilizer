package org.accessibilizer.pdf;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.itextpdf.kernel.exceptions.BadPasswordException;
import com.itextpdf.kernel.exceptions.PdfException;
import com.itextpdf.kernel.font.PdfFont;
import com.itextpdf.kernel.font.PdfFontFactory;
import com.itextpdf.kernel.geom.PageSize;
import com.itextpdf.kernel.geom.Rectangle;
import com.itextpdf.kernel.pdf.PdfDocument;
import com.itextpdf.kernel.pdf.PdfArray;
import com.itextpdf.kernel.pdf.PdfDictionary;
import com.itextpdf.kernel.pdf.PdfName;
import com.itextpdf.kernel.pdf.PdfObject;
import com.itextpdf.kernel.pdf.PdfOutline;
import com.itextpdf.kernel.pdf.PdfPage;
import com.itextpdf.kernel.pdf.PdfReader;
import com.itextpdf.kernel.pdf.PdfString;
import com.itextpdf.kernel.pdf.PdfUAConformance;
import com.itextpdf.kernel.pdf.PdfWriter;
import com.itextpdf.kernel.pdf.action.PdfAction;
import com.itextpdf.kernel.pdf.annot.PdfAnnotation;
import com.itextpdf.kernel.pdf.annot.PdfLinkAnnotation;
import com.itextpdf.kernel.pdf.canvas.CanvasArtifact;
import com.itextpdf.kernel.pdf.canvas.PdfCanvas;
import com.itextpdf.kernel.pdf.canvas.PdfCanvasConstants.TextRenderingMode;
import com.itextpdf.kernel.pdf.navigation.PdfExplicitDestination;
import com.itextpdf.kernel.pdf.tagging.PdfStructureAttributes;
import com.itextpdf.kernel.pdf.tagging.StandardRoles;
import com.itextpdf.kernel.pdf.tagging.IStructureNode;
import com.itextpdf.kernel.pdf.tagging.PdfStructElem;
import com.itextpdf.layout.Canvas;
import com.itextpdf.layout.borders.Border;
import com.itextpdf.layout.element.Cell;
import com.itextpdf.layout.element.Div;
import com.itextpdf.layout.element.Link;
import com.itextpdf.layout.element.Paragraph;
import com.itextpdf.layout.element.Table;
import com.itextpdf.layout.properties.Property;
import com.itextpdf.pdfua.PdfUAConfig;
import com.itextpdf.pdfua.PdfUADocument;

import java.io.IOException;
import java.io.Reader;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Deque;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class Author {
    private static final Path FONT = Path.of("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf");

    private Author() {
    }

    public static void main(String[] arguments) throws Exception {
        if (arguments.length == 2 && arguments[0].equals("--preflight")) {
            System.out.println(new Gson().toJson(preflight(Path.of(arguments[1]))));
            return;
        }
        if (arguments.length == 2 && arguments[0].equals("--inspect")) {
            System.out.println(new Gson().toJson(inspect(Path.of(arguments[1]))));
            return;
        }
        if (arguments.length != 3) {
            throw new IllegalArgumentException(
                    "usage: pdf-author CONTRACT SOURCE OUTPUT | "
                            + "pdf-author --preflight SOURCE | pdf-author --inspect OUTPUT");
        }
        JsonObject contract = readContract(Path.of(arguments[0]));
        author(contract, Path.of(arguments[1]), Path.of(arguments[2]));
    }

    private static JsonObject preflight(Path sourcePath) throws IOException {
        JsonArray unsupportedFeatures = new JsonArray();
        try (PdfDocument document = new PdfDocument(new PdfReader(sourcePath.toString()))) {
            if (document.getReader().isEncrypted()) {
                unsupportedFeatures.add("encryption");
            }
            for (int objectNumber = 1;
                    objectNumber < document.getNumberOfPdfObjects();
                    objectNumber++) {
                PdfObject object = document.getPdfObject(objectNumber);
                inspectObject(
                        object,
                        unsupportedFeatures,
                        Collections.newSetFromMap(new IdentityHashMap<>()));
            }
        } catch (BadPasswordException error) {
            unsupportedFeatures.add("encryption");
        } catch (PdfException error) {
            if (error.getMessage() != null && error.getMessage().contains("PdfEncryption")) {
                unsupportedFeatures.add("encryption");
            } else {
                throw error;
            }
        }
        JsonObject result = new JsonObject();
        result.add("unsupported_features", unsupportedFeatures);
        return result;
    }

    private static void inspectObject(
            PdfObject object, JsonArray unsupportedFeatures, Set<PdfObject> visited) {
        if (object == null || object.isIndirectReference() || !visited.add(object)) {
            return;
        }
        if (object instanceof PdfDictionary dictionary) {
            inspectDictionary(dictionary, unsupportedFeatures);
            for (var entry : dictionary.entrySet()) {
                inspectObject(entry.getValue(), unsupportedFeatures, visited);
            }
        } else if (object instanceof PdfArray array) {
            for (int index = 0; index < array.size(); index++) {
                inspectObject(array.get(index, false), unsupportedFeatures, visited);
            }
        }
    }

    private static void inspectDictionary(
            PdfDictionary dictionary, JsonArray unsupportedFeatures) {
        if (dictionary.containsKey(new PdfName("AcroForm"))) {
            addFinding(unsupportedFeatures, "form fields");
        }
        if (dictionary.containsKey(new PdfName("JavaScript"))) {
            addFinding(unsupportedFeatures, "JavaScript");
        }
        if (dictionary.containsKey(new PdfName("EmbeddedFiles"))
                || dictionary.containsKey(new PdfName("AF"))) {
            addFinding(unsupportedFeatures, "embedded files");
        }
        if (dictionary.containsKey(new PdfName("OpenAction"))
                || dictionary.containsKey(new PdfName("AA"))) {
            addFinding(unsupportedFeatures, "automatic or additional actions");
        }

        PdfName fieldType = dictionary.getAsName(new PdfName("FT"));
        PdfName type = dictionary.getAsName(PdfName.Type);
        if (PdfName.Sig.equals(fieldType) || PdfName.Sig.equals(type)
                || dictionary.containsKey(new PdfName("ByteRange"))) {
            addFinding(unsupportedFeatures, "digital signatures");
        }

        PdfName action = dictionary.getAsName(PdfName.S);
        Set<String> unsupportedActions = Set.of(
                "JavaScript", "Launch", "Sound", "Movie", "Rendition", "GoToE", "GoToR",
                "GoTo", "URI", "Named", "SubmitForm", "ResetForm", "ImportData", "Hide",
                "SetOCGState", "Trans");
        if (action != null && unsupportedActions.contains(action.getValue())) {
            addFinding(unsupportedFeatures, "interactive action " + action.getValue());
        }

        PdfName subtype = dictionary.getAsName(PdfName.Subtype);
        Set<String> unsupportedAnnotations = Set.of(
                "Widget", "Link", "FileAttachment", "Sound", "Movie", "Screen", "RichMedia",
                "3D");
        if (subtype != null && unsupportedAnnotations.contains(subtype.getValue())) {
            addFinding(unsupportedFeatures, "interactive annotation " + subtype.getValue());
        }
    }

    private static void addFinding(JsonArray findings, String finding) {
        for (var existing : findings) {
            if (existing.getAsString().equals(finding)) {
                return;
            }
        }
        findings.add(finding);
    }

    private static JsonObject readContract(Path path) throws IOException {
        try (Reader reader = Files.newBufferedReader(path)) {
            return new Gson().fromJson(reader, JsonObject.class);
        }
    }

    // A heading recorded while authoring, so the document outline can be built from
    // the heading hierarchy after every page is on the document: the level drives the
    // nesting and the output page is the destination of that outline entry.
    private record HeadingRef(int level, String text, PdfPage page) {
    }

    // An open outline frame while nesting the tree: an H(n) becomes a child of the most
    // recent preceding heading with level < n, so frames whose level is >= the incoming
    // heading are popped before its parent is read off the top of the stack.
    private record OutlineFrame(int level, PdfOutline outline) {
    }

    private static void author(JsonObject contract, Path sourcePath, Path outputPath) throws Exception {
        String title = requiredString(contract, "title");
        String language = requiredString(contract, "language");
        JsonArray pages = contract.getAsJsonArray("pages");

        try (PdfDocument source = new PdfDocument(new PdfReader(sourcePath.toString()));
             PdfUADocument output = new PdfUADocument(
                     new PdfWriter(outputPath.toString()),
                     new PdfUAConfig(PdfUAConformance.PDF_UA_1, title, language))) {
            // One authoring font shared across every page rather than re-created per page.
            PdfFont font = PdfFontFactory.createFont(FONT.toString());
            List<HeadingRef> headings = new ArrayList<>();
            PdfPage firstOutputPage = null;

            for (JsonElement pageElement : pages) {
                JsonObject pageNode = pageElement.getAsJsonObject();
                int sourcePageNumber = pageNode.get("source_page").getAsInt();
                if (sourcePageNumber < 1 || sourcePageNumber > source.getNumberOfPages()) {
                    throw new IllegalArgumentException("source page is outside the document");
                }

                var sourcePage = source.getPage(sourcePageNumber);
                var outputPage = output.addNewPage(new PageSize(sourcePage.getPageSize()));
                outputPage.setTabOrder(PdfName.S);
                if (firstOutputPage == null) {
                    firstOutputPage = outputPage;
                }

                var visualLayer = sourcePage.copyAsFormXObject(output);
                PdfCanvas visualCanvas = new PdfCanvas(outputPage);
                visualCanvas.openTag(new CanvasArtifact());
                visualCanvas.addXObjectAt(visualLayer, 0, 0);
                visualCanvas.closeTag();

                addSemanticLayer(outputPage, font, pageNode.getAsJsonArray("semantic_layer"), headings);
            }

            buildOutline(output, title, headings, firstOutputPage);
        }
    }

    // The document outline is built from the heading hierarchy collected across every
    // page: each H(n) nests under the most recent preceding heading with a smaller
    // level, and its destination is a fit-to-page GoTo of that heading's output page.
    // With no headings at all a single top-level entry titled by the document title
    // points at the first page, matching the pre-hierarchy behaviour.
    private static void buildOutline(
            PdfUADocument output, String title, List<HeadingRef> headings, PdfPage firstOutputPage) {
        PdfOutline root = output.getOutlines(false);
        if (headings.isEmpty()) {
            PdfOutline bookmark = root.addOutline(title);
            bookmark.addAction(PdfAction.createGoTo(PdfExplicitDestination.createFit(firstOutputPage)));
            return;
        }

        Deque<OutlineFrame> stack = new ArrayDeque<>();
        for (HeadingRef heading : headings) {
            while (!stack.isEmpty() && stack.peek().level() >= heading.level()) {
                stack.pop();
            }
            PdfOutline parent = stack.isEmpty() ? root : stack.peek().outline();
            PdfOutline entry = parent.addOutline(heading.text());
            entry.addAction(PdfAction.createGoTo(PdfExplicitDestination.createFit(heading.page())));
            stack.push(new OutlineFrame(heading.level(), entry));
        }
    }

    // The Semantic Layer is authored as real text drawn at a readable size across
    // the page with text rendering mode 3, so it produces no marks on screen or in
    // the print path. macOS Preview derives its accessibility text from the glyphs
    // physically laid out on the page, so the glyphs must spell the complete
    // strings at full width rather than the clipped fragments produced by the
    // one-point-wide overlay that ADR 0026 rejected. Each node occupies its own
    // vertical band, top to bottom in Logical Reading Order, so no run overlaps
    // another. The Formula draws its normalized math. Preview reads a Figure's
    // /Alt and ignores that element's own glyphs and ActualText, so the Figure
    // carries the short Alternative in /Alt while a complex figure's Detailed Figure
    // Description is authored as a sibling Caption whose glyphs Preview reads like any
    // other text element; a simple figure has only the Alternative and no Caption.
    // ActualText and Alt remain on every structure element so the
    // internal extraction and PDF/UA gates are unaffected, and the Figure is
    // attached to a real glyph run instead of an empty container so Preview
    // cannot drop it. A Semantic Table is the other exception: rather than a single
    // band it is authored as a real PDF/UA Table (see addTable) so its caption,
    // headers, cells, header associations, and merged-cell spans reach the structure.
    private static final float PAGE_MARGIN = 40f;
    private static final float SEMANTIC_FONT_SIZE = 10f;
    private static final float FIGURE_CAPTION_GAP = 24f;

    private static void addSemanticLayer(
            PdfPage page, PdfFont font, JsonArray nodes, List<HeadingRef> headings) {
        Rectangle pageSize = page.getPageSize();
        float usableWidth = pageSize.getWidth() - 2 * PAGE_MARGIN;
        int count = nodes.size();
        float bandHeight = count == 0 ? 0 : (pageSize.getHeight() - 2 * PAGE_MARGIN) / count;
        try (Canvas canvas = new Canvas(page, pageSize)) {
            for (int index = 0; index < count; index++) {
                JsonObject node = nodes.get(index).getAsJsonObject();
                float bandBottom =
                        pageSize.getTop() - PAGE_MARGIN - (index + 0.5f) * bandHeight;
                String type = requiredString(node, "type");
                switch (type) {
                    case "heading" -> {
                        int level = node.get("level").getAsInt();
                        if (level < 1 || level > 6) {
                            throw new IllegalArgumentException("heading level out of range: " + level);
                        }
                        String text = requiredString(node, "text");
                        // Role is H1..H6 by level; StandardRoles.H1 is the string "H1".
                        addNode(canvas, font, "H" + level, text, text, null,
                                usableWidth, bandBottom);
                        // Recorded so the document outline can be nested by heading level.
                        headings.add(new HeadingRef(level, text, page));
                    }
                    case "paragraph" -> {
                        String text = requiredString(node, "text");
                        addNode(canvas, font, StandardRoles.P, text, text, null,
                                usableWidth, bandBottom);
                    }
                    case "formula" -> {
                        String math = requiredString(node, "normalized_math");
                        String spoken = requiredString(node, "spoken_math_alternative");
                        addNode(canvas, font, StandardRoles.FORMULA, math, math, spoken,
                                usableWidth, bandBottom);
                    }
                    case "figure" -> {
                        String alternative = requiredString(node, "figure_alternative");
                        JsonElement detailedElement = node.get("detailed_figure_description");
                        if (detailedElement != null && detailedElement.isJsonPrimitive()) {
                            // A complex figure carries its Detailed Figure Description
                            // on ActualText and as a sibling Caption Preview can read.
                            String detailed = detailedElement.getAsString();
                            addNode(canvas, font, StandardRoles.FIGURE,
                                    alternative, detailed, alternative,
                                    usableWidth, bandBottom + FIGURE_CAPTION_GAP);
                            addNode(canvas, font, StandardRoles.CAPTION,
                                    detailed, detailed, null,
                                    usableWidth, bandBottom - FIGURE_CAPTION_GAP);
                        } else {
                            // A simple figure carries only its concise Figure
                            // Alternative, with no Detailed Figure Description or Caption.
                            addNode(canvas, font, StandardRoles.FIGURE,
                                    alternative, alternative, alternative,
                                    usableWidth, bandBottom);
                        }
                    }
                    case "table" -> addTable(canvas, font, node, usableWidth, bandBottom);
                    case "link" -> {
                        String text = requiredString(node, "text");
                        String href = requiredString(node, "href");
                        addLink(canvas, font, text, href, usableWidth, bandBottom);
                    }
                    default -> throw new IllegalArgumentException("unsupported semantic node: " + type);
                }
            }
        }
    }

    private static void addNode(
            Canvas canvas, PdfFont font, String role,
            String laidOutText, String actualText, String alternateDescription,
            float width, float bottom) {
        Paragraph paragraph = new Paragraph(sanitizeForFont(font, laidOutText))
                .setFont(font)
                .setFontSize(SEMANTIC_FONT_SIZE)
                .setMargin(0)
                .setMultipliedLeading(1f)
                .setFixedPosition(PAGE_MARGIN, bottom, width);
        paragraph.setProperty(Property.TEXT_RENDERING_MODE, TextRenderingMode.INVISIBLE);
        paragraph.getAccessibilityProperties().setRole(role).setActualText(actualText);
        if (alternateDescription != null) {
            paragraph.getAccessibilityProperties().setAlternateDescription(alternateDescription);
        }
        canvas.add(paragraph);
    }

    // A Link is authored as a Link structure element wrapped in a Paragraph (role P),
    // exactly like every other node's full-width invisible glyph run, but its glyphs
    // belong to an iText layout Link bound to a Link annotation whose action opens the
    // URI. The Link element carries ActualText and an alternate description (both the
    // link text) on the structure element. PDF/UA additionally requires the link
    // annotation itself to expose an alternate description, so the annotation's
    // /Contents is set to the text; the annotation is flagged Print and given a zero
    // border so it neither prints a box nor is dropped from the print path.
    private static void addLink(
            Canvas canvas, PdfFont font, String text, String href, float width, float bottom) {
        PdfLinkAnnotation annotation = new PdfLinkAnnotation(new Rectangle(0, 0, 0, 0));
        annotation.setAction(PdfAction.createURI(href));
        annotation.setContents(text);
        annotation.setFlag(PdfAnnotation.PRINT);
        annotation.put(PdfName.Border, new PdfArray(new int[] {0, 0, 0}));

        Link link = new Link(sanitizeForFont(font, text), annotation);
        link.setFont(font).setFontSize(SEMANTIC_FONT_SIZE);
        link.setProperty(Property.TEXT_RENDERING_MODE, TextRenderingMode.INVISIBLE);
        link.getAccessibilityProperties()
                .setRole(StandardRoles.LINK)
                .setActualText(text)
                .setAlternateDescription(text);

        Paragraph paragraph = new Paragraph(link)
                .setFont(font)
                .setFontSize(SEMANTIC_FONT_SIZE)
                .setMargin(0)
                .setMultipliedLeading(1f)
                .setFixedPosition(PAGE_MARGIN, bottom, width);
        paragraph.setProperty(Property.TEXT_RENDERING_MODE, TextRenderingMode.INVISIBLE);
        canvas.add(paragraph);
    }

    // A Semantic Table is authored as a real PDF/UA Table so its caption, row and
    // column headers, cells, merged-cell spans, and header associations reach the
    // structure tree: the Table's optional Caption carries the caption; each cell is
    // a TH or TD whose ActualText holds the cell text; a header cell adds a Scope
    // attribute (Row, Column, or Both) that associates the cells it labels; and a
    // merged cell keeps its RowSpan and ColSpan. Like every other node the glyphs are
    // drawn with text rendering mode 3 and no cell borders, so the table adds no marks
    // to the Visual Layer while the tagged structure remains fully conformant.
    private static void addTable(
            Canvas canvas, PdfFont font, JsonObject node, float width, float bottom) {
        JsonArray rows = node.getAsJsonArray("rows");
        int columnCount = 0;
        for (JsonElement rowElement : rows) {
            int columns = 0;
            for (JsonElement cellElement : rowElement.getAsJsonObject().getAsJsonArray("cells")) {
                columns += cellElement.getAsJsonObject().get("col_span").getAsInt();
            }
            columnCount = Math.max(columnCount, columns);
        }

        Table table = new Table(columnCount);
        table.setFont(font).setFontSize(SEMANTIC_FONT_SIZE).setBorder(Border.NO_BORDER);
        table.setProperty(Property.TEXT_RENDERING_MODE, TextRenderingMode.INVISIBLE);
        table.setFixedPosition(PAGE_MARGIN, bottom, width);

        JsonElement caption = node.get("caption");
        if (caption != null && caption.isJsonPrimitive()) {
            String captionText = caption.getAsString();
            Div captionDiv = new Div().add(tableParagraph(font, captionText));
            captionDiv.getAccessibilityProperties()
                    .setRole(StandardRoles.CAPTION)
                    .setActualText(captionText);
            table.setCaption(captionDiv);
        }

        for (JsonElement rowElement : rows) {
            for (JsonElement cellElement : rowElement.getAsJsonObject().getAsJsonArray("cells")) {
                JsonObject cellNode = cellElement.getAsJsonObject();
                String kind = requiredString(cellNode, "kind");
                String text = requiredString(cellNode, "text");
                Cell cell = new Cell(cellNode.get("row_span").getAsInt(), cellNode.get("col_span").getAsInt());
                cell.setBorder(Border.NO_BORDER).setMargin(0).setPadding(0);
                cell.add(tableParagraph(font, text));
                boolean isHeader = kind.equals("header");
                cell.getAccessibilityProperties()
                        .setRole(isHeader ? StandardRoles.TH : StandardRoles.TD)
                        .setActualText(text);
                if (isHeader) {
                    // Scope associates a header cell with the cells it labels; without
                    // it a PDF/UA table cell would have no header relationship.
                    cell.getAccessibilityProperties().addAttributes(
                            new PdfStructureAttributes("Table")
                                    .addEnumAttribute("Scope", scopePdfValue(requiredString(cellNode, "scope"))));
                }
                table.addCell(cell);
            }
        }
        canvas.add(table);
    }

    private static Paragraph tableParagraph(PdfFont font, String text) {
        Paragraph paragraph = new Paragraph(sanitizeForFont(font, text))
                .setFont(font)
                .setFontSize(SEMANTIC_FONT_SIZE)
                .setMargin(0)
                .setMultipliedLeading(1f);
        // Text rendering mode is not inherited from the Table, so each cell's glyph
        // run is made invisible here — it adds no marks to the Visual Layer.
        paragraph.setProperty(Property.TEXT_RENDERING_MODE, TextRenderingMode.INVISIBLE);
        return paragraph;
    }

    private static String scopePdfValue(String scope) {
        return switch (scope) {
            case "col" -> "Column";
            case "row" -> "Row";
            case "both" -> "Both";
            default -> throw new IllegalArgumentException("invalid header scope: " + scope);
        };
    }

    // The meaning of a node is carried by its ActualText and Alt, which are PDF
    // text strings independent of the font, so a Formula's fractions,
    // superscripts, subscripts, symbols, and units always survive to the tagged
    // structure and to text extraction. The laid-out glyph run is only what a
    // sighted reader would see and what macOS Preview reads for prose nodes, and
    // PDF/UA forbids the .notdef glyph. Any character the authoring font cannot
    // render is therefore dropped from the invisible laid-out run (replaced with a
    // space) so authoring never emits .notdef, while ActualText and Alt keep the
    // exact string. English prose is fully covered by the font, so headings and
    // paragraphs are unaffected; only exotic mathematical symbols are ever
    // substituted, and their exact form is still exposed through ActualText.
    private static String sanitizeForFont(PdfFont font, String text) {
        StringBuilder builder = new StringBuilder(text.length());
        int index = 0;
        while (index < text.length()) {
            int codePoint = text.codePointAt(index);
            index += Character.charCount(codePoint);
            if (font.containsGlyph(codePoint) || Character.isWhitespace(codePoint)) {
                builder.appendCodePoint(codePoint);
            } else {
                builder.append(' ');
            }
        }
        String sanitized = builder.toString();
        return sanitized.isBlank() ? " " : sanitized;
    }

    private static JsonObject inspect(Path outputPath) throws IOException {
        JsonArray pages = new JsonArray();
        try (PdfDocument document = new PdfDocument(new PdfReader(outputPath.toString()))) {
            // One Semantic Layer per output page, keyed by page number and kept in
            // document (page 1 first) order; the tree walk fills each page in the
            // order its nodes appear beneath the structure tree root.
            Map<Integer, JsonArray> byPage = new LinkedHashMap<>();
            for (int number = 1; number <= document.getNumberOfPages(); number++) {
                byPage.put(number, new JsonArray());
            }
            collectSemanticNodes(document, document.getStructTreeRoot().getKids(), byPage);
            for (int number = 1; number <= document.getNumberOfPages(); number++) {
                JsonObject page = new JsonObject();
                page.add("semantic_layer", byPage.get(number));
                pages.add(page);
            }
        }
        JsonObject result = new JsonObject();
        result.add("pages", pages);
        return result;
    }

    private static void collectSemanticNodes(
            PdfDocument document, List<IStructureNode> nodes, Map<Integer, JsonArray> byPage) {
        for (IStructureNode structureNode : nodes) {
            if (!(structureNode instanceof PdfStructElem element)) {
                continue;
            }
            String role = element.getRole().getValue();
            JsonObject extracted;
            if (isHeadingRole(role)) {
                extracted = heading(element, role);
            } else if (role.equals(StandardRoles.P)) {
                // A link is authored as a Link structure element inside a Paragraph, so a
                // P is a link when a Link descendant exists and a paragraph otherwise.
                PdfStructElem linkElement = findLink(element);
                extracted = linkElement != null
                        ? linkNode(document, linkElement)
                        : textNode("paragraph", element);
            } else if (role.equals(StandardRoles.FORMULA)) {
                extracted = formula(element);
            } else if (role.equals(StandardRoles.FIGURE)) {
                extracted = figure(element);
            } else if (role.equals(StandardRoles.TABLE)) {
                extracted = table(element);
            } else {
                extracted = null;
            }
            if (extracted != null) {
                // A semantic node owns its whole subtree (a Table's rows and cells, a
                // node's laid-out glyph run), so recursion stops here; only structural
                // containers are traversed to reach the flat Semantic Layer beneath.
                PdfDictionary pageDict = pageObjectOf(element);
                int pageNumber = pageDict == null ? 1 : document.getPageNumber(pageDict);
                byPage.computeIfAbsent(pageNumber, key -> new JsonArray()).add(extracted);
            } else {
                collectSemanticNodes(document, element.getKids(), byPage);
            }
        }
    }

    // The output page a structure element sits on is its own /Pg when present, else the
    // first /Pg found on a struct-element descendant.
    private static PdfDictionary pageObjectOf(PdfStructElem element) {
        PdfDictionary page = element.getPdfObject().getAsDictionary(PdfName.Pg);
        if (page != null) {
            return page;
        }
        for (IStructureNode kid : element.getKids()) {
            if (kid instanceof PdfStructElem child) {
                PdfDictionary childPage = pageObjectOf(child);
                if (childPage != null) {
                    return childPage;
                }
            }
        }
        return null;
    }

    private static boolean isHeadingRole(String role) {
        return role.length() == 2 && role.charAt(0) == 'H'
                && role.charAt(1) >= '1' && role.charAt(1) <= '6';
    }

    // A Link structure element nested anywhere beneath a Paragraph, or null when the
    // Paragraph is plain prose.
    private static PdfStructElem findLink(PdfStructElem element) {
        for (IStructureNode kid : element.getKids()) {
            if (kid instanceof PdfStructElem child) {
                if (child.getRole().getValue().equals(StandardRoles.LINK)) {
                    return child;
                }
                PdfStructElem nested = findLink(child);
                if (nested != null) {
                    return nested;
                }
            }
        }
        return null;
    }

    private static JsonObject linkNode(PdfDocument document, PdfStructElem linkElement) {
        JsonObject node = new JsonObject();
        node.addProperty("type", "link");
        node.addProperty("text", structureString(linkElement, PdfName.ActualText));
        node.addProperty("href", linkHref(document, linkElement));
        return node;
    }

    // The link's destination is read from the Link annotation on the Link element's
    // page: the first Link-subtype annotation's /A action /URI. This assumes at most
    // one link per page (true for every node the reconstruction emits); associating a
    // specific Link element with its own annotation would need the element's OBJR.
    private static String linkHref(PdfDocument document, PdfStructElem linkElement) {
        PdfDictionary pageDict = pageObjectOf(linkElement);
        if (pageDict == null) {
            return "";
        }
        PdfPage page = document.getPage(document.getPageNumber(pageDict));
        for (PdfAnnotation annotation : page.getAnnotations()) {
            if (PdfName.Link.equals(annotation.getSubtype())) {
                PdfDictionary action = annotation.getPdfObject().getAsDictionary(PdfName.A);
                if (action != null) {
                    PdfString uri = action.getAsString(PdfName.URI);
                    if (uri != null) {
                        return uri.toUnicodeString();
                    }
                }
            }
        }
        return "";
    }

    private static JsonObject heading(PdfStructElem element, String role) {
        JsonObject node = textNode("heading", element);
        // Level is the digit in the H1..H6 role.
        node.addProperty("level", role.charAt(1) - '0');
        return node;
    }

    private static JsonObject textNode(String type, PdfStructElem element) {
        JsonObject node = new JsonObject();
        node.addProperty("type", type);
        node.addProperty("text", structureString(element, PdfName.ActualText));
        return node;
    }

    private static JsonObject formula(PdfStructElem element) {
        JsonObject node = new JsonObject();
        node.addProperty("type", "formula");
        node.addProperty("normalized_math", structureString(element, PdfName.ActualText));
        node.addProperty("spoken_math_alternative", structureString(element, PdfName.Alt));
        return node;
    }

    private static JsonObject figure(PdfStructElem element) {
        String alternative = structureString(element, PdfName.Alt);
        String actualText = structureString(element, PdfName.ActualText);
        JsonObject node = new JsonObject();
        node.addProperty("type", "figure");
        node.addProperty("figure_alternative", alternative);
        // A complex figure's ActualText holds a Detailed Figure Description distinct
        // from its concise Alternative; a simple figure repeats the Alternative and
        // exposes no Detailed Figure Description.
        if (!actualText.isEmpty() && !actualText.equals(alternative)) {
            node.addProperty("complexity", "complex");
            node.addProperty("detailed_figure_description", actualText);
        } else {
            node.addProperty("complexity", "simple");
        }
        return node;
    }

    private static JsonObject table(PdfStructElem element) {
        JsonObject node = new JsonObject();
        node.addProperty("type", "table");
        String caption = tableCaption(element);
        if (caption != null) {
            node.addProperty("caption", caption);
        }
        JsonArray rows = new JsonArray();
        List<PdfStructElem> rowElements = new ArrayList<>();
        collectTableRows(element.getKids(), rowElements);
        for (PdfStructElem rowElement : rowElements) {
            JsonArray cells = new JsonArray();
            for (IStructureNode kid : rowElement.getKids()) {
                if (!(kid instanceof PdfStructElem cellElement)) {
                    continue;
                }
                String role = cellElement.getRole().getValue();
                if (role.equals(StandardRoles.TH) || role.equals(StandardRoles.TD)) {
                    cells.add(tableCell(cellElement, role.equals(StandardRoles.TH)));
                }
            }
            JsonObject row = new JsonObject();
            row.add("cells", cells);
            rows.add(row);
        }
        node.add("rows", rows);
        return node;
    }

    // Rows may sit directly under the Table or be grouped under a THead, TBody, or
    // TFoot; either way they are collected top to bottom in document order.
    private static void collectTableRows(List<IStructureNode> kids, List<PdfStructElem> rows) {
        for (IStructureNode kid : kids) {
            if (!(kid instanceof PdfStructElem element)) {
                continue;
            }
            String role = element.getRole().getValue();
            if (role.equals(StandardRoles.TR)) {
                rows.add(element);
            } else if (role.equals(StandardRoles.THEAD)
                    || role.equals(StandardRoles.TBODY)
                    || role.equals(StandardRoles.TFOOT)) {
                collectTableRows(element.getKids(), rows);
            }
        }
    }

    private static JsonObject tableCell(PdfStructElem element, boolean isHeader) {
        JsonObject cell = new JsonObject();
        cell.addProperty("kind", isHeader ? "header" : "data");
        cell.addProperty("text", structureString(element, PdfName.ActualText));
        cell.addProperty("scope", isHeader ? scopeFromPdf(tableAttributeEnum(element, "Scope")) : "none");
        cell.addProperty("row_span", tableAttributeInt(element, "RowSpan"));
        cell.addProperty("col_span", tableAttributeInt(element, "ColSpan"));
        return cell;
    }

    private static String tableCaption(PdfStructElem element) {
        for (IStructureNode kid : element.getKids()) {
            if (kid instanceof PdfStructElem child
                    && child.getRole().getValue().equals(StandardRoles.CAPTION)) {
                return structureString(child, PdfName.ActualText);
            }
        }
        return null;
    }

    private static String scopeFromPdf(String value) {
        if (value == null) {
            return "none";
        }
        return switch (value) {
            case "Column" -> "col";
            case "Row" -> "row";
            case "Both" -> "both";
            default -> "none";
        };
    }

    private static String tableAttributeEnum(PdfStructElem element, String name) {
        for (PdfStructureAttributes attributes : element.getAttributesList()) {
            String value = attributes.getAttributeAsEnum(name);
            if (value != null) {
                return value;
            }
        }
        return null;
    }

    // A cell that is not merged carries no RowSpan or ColSpan attribute; its span is 1.
    private static int tableAttributeInt(PdfStructElem element, String name) {
        for (PdfStructureAttributes attributes : element.getAttributesList()) {
            Integer value = attributes.getAttributeAsInt(name);
            if (value != null) {
                return value;
            }
        }
        return 1;
    }

    private static String structureString(PdfStructElem element, PdfName key) {
        PdfString value = element.getPdfObject().getAsString(key);
        return value == null ? "" : value.toUnicodeString();
    }

    private static String requiredString(JsonObject object, String field) {
        if (!object.has(field) || !object.get(field).isJsonPrimitive()) {
            throw new IllegalArgumentException("missing string field: " + field);
        }
        return object.get(field).getAsString();
    }
}
