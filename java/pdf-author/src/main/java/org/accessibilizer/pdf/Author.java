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
import com.itextpdf.kernel.pdf.PdfReader;
import com.itextpdf.kernel.pdf.PdfString;
import com.itextpdf.kernel.pdf.PdfUAConformance;
import com.itextpdf.kernel.pdf.PdfWriter;
import com.itextpdf.kernel.pdf.action.PdfAction;
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
import com.itextpdf.layout.element.Paragraph;
import com.itextpdf.layout.element.Table;
import com.itextpdf.layout.properties.Property;
import com.itextpdf.pdfua.PdfUAConfig;
import com.itextpdf.pdfua.PdfUADocument;

import java.io.IOException;
import java.io.Reader;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
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

    private static void author(JsonObject contract, Path sourcePath, Path outputPath) throws Exception {
        String title = requiredString(contract, "title");
        String language = requiredString(contract, "language");
        int sourcePageNumber = contract.get("page").getAsInt();

        try (PdfDocument source = new PdfDocument(new PdfReader(sourcePath.toString()));
             PdfUADocument output = new PdfUADocument(
                     new PdfWriter(outputPath.toString()),
                     new PdfUAConfig(PdfUAConformance.PDF_UA_1, title, language))) {
            if (sourcePageNumber < 1 || sourcePageNumber > source.getNumberOfPages()) {
                throw new IllegalArgumentException("source page is outside the document");
            }

            var sourcePage = source.getPage(sourcePageNumber);
            var outputPage = output.addNewPage(new PageSize(sourcePage.getPageSize()));
            outputPage.setTabOrder(PdfName.S);

            var visualLayer = sourcePage.copyAsFormXObject(output);
            PdfCanvas visualCanvas = new PdfCanvas(outputPage);
            visualCanvas.openTag(new CanvasArtifact());
            visualCanvas.addXObjectAt(visualLayer, 0, 0);
            visualCanvas.closeTag();

            PdfFont font = PdfFontFactory.createFont(FONT.toString());
            addSemanticLayer(outputPage, font, contract.getAsJsonArray("semantic_layer"));

            PdfOutline bookmark = output.getOutlines(false).addOutline(title);
            bookmark.addAction(PdfAction.createGoTo(PdfExplicitDestination.createFit(outputPage)));
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
            com.itextpdf.kernel.pdf.PdfPage page, PdfFont font, JsonArray nodes) {
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
                        String text = requiredString(node, "text");
                        addNode(canvas, font, StandardRoles.H1, text, text, null,
                                usableWidth, bandBottom);
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
        JsonArray semanticLayer = new JsonArray();
        try (PdfDocument document = new PdfDocument(new PdfReader(outputPath.toString()))) {
            collectSemanticNodes(document.getStructTreeRoot().getKids(), semanticLayer);
        }
        JsonObject result = new JsonObject();
        result.add("semantic_layer", semanticLayer);
        return result;
    }

    private static void collectSemanticNodes(List<IStructureNode> nodes, JsonArray result) {
        for (IStructureNode structureNode : nodes) {
            if (!(structureNode instanceof PdfStructElem element)) {
                continue;
            }
            String role = element.getRole().getValue();
            JsonObject extracted = switch (role) {
                case StandardRoles.H1 -> heading(element);
                case StandardRoles.P -> textNode("paragraph", element);
                case StandardRoles.FORMULA -> formula(element);
                case StandardRoles.FIGURE -> figure(element);
                case StandardRoles.TABLE -> table(element);
                default -> null;
            };
            if (extracted != null) {
                // A semantic node owns its whole subtree (a Table's rows and cells, a
                // node's laid-out glyph run), so recursion stops here; only structural
                // containers are traversed to reach the flat Semantic Layer beneath.
                result.add(extracted);
            } else {
                collectSemanticNodes(element.getKids(), result);
            }
        }
    }

    private static JsonObject heading(PdfStructElem element) {
        JsonObject node = textNode("heading", element);
        node.addProperty("level", 1);
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
