package org.accessibilizer.pdf;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
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
import com.itextpdf.kernel.pdf.tagging.StandardRoles;
import com.itextpdf.kernel.pdf.tagging.IStructureNode;
import com.itextpdf.kernel.pdf.tagging.PdfStructElem;
import com.itextpdf.layout.Canvas;
import com.itextpdf.layout.element.Paragraph;
import com.itextpdf.layout.properties.Property;
import com.itextpdf.pdfua.PdfUAConfig;
import com.itextpdf.pdfua.PdfUADocument;

import java.io.IOException;
import java.io.Reader;
import java.nio.file.Files;
import java.nio.file.Path;
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
    // carries the short Alternative in /Alt while the Detailed Figure Description
    // is authored as a sibling Caption whose glyphs Preview reads like any other
    // text element. ActualText and Alt remain on every structure element so the
    // internal extraction and PDF/UA gates are unaffected, and the Figure is
    // attached to a real glyph run instead of an empty container so Preview
    // cannot drop it.
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
                        String detailed = requiredString(node, "detailed_figure_description");
                        addNode(canvas, font, StandardRoles.FIGURE,
                                alternative, detailed, alternative,
                                usableWidth, bandBottom + FIGURE_CAPTION_GAP);
                        addNode(canvas, font, StandardRoles.CAPTION,
                                detailed, detailed, null,
                                usableWidth, bandBottom - FIGURE_CAPTION_GAP);
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
        Paragraph paragraph = new Paragraph(laidOutText)
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
                default -> null;
            };
            if (extracted != null) {
                result.add(extracted);
            }
            collectSemanticNodes(element.getKids(), result);
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
        JsonObject node = new JsonObject();
        node.addProperty("type", "figure");
        node.addProperty("figure_alternative", structureString(element, PdfName.Alt));
        node.addProperty(
                "detailed_figure_description", structureString(element, PdfName.ActualText));
        return node;
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
