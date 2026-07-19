package org.accessibilizer.pdf;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.itextpdf.kernel.font.PdfFont;
import com.itextpdf.kernel.font.PdfFontFactory;
import com.itextpdf.kernel.geom.PageSize;
import com.itextpdf.kernel.pdf.PdfDocument;
import com.itextpdf.kernel.pdf.PdfName;
import com.itextpdf.kernel.pdf.PdfOutline;
import com.itextpdf.kernel.pdf.PdfReader;
import com.itextpdf.kernel.pdf.PdfString;
import com.itextpdf.kernel.pdf.PdfUAConformance;
import com.itextpdf.kernel.pdf.PdfWriter;
import com.itextpdf.kernel.pdf.action.PdfAction;
import com.itextpdf.kernel.pdf.canvas.CanvasArtifact;
import com.itextpdf.kernel.pdf.canvas.PdfCanvas;
import com.itextpdf.kernel.pdf.navigation.PdfExplicitDestination;
import com.itextpdf.kernel.pdf.tagging.StandardRoles;
import com.itextpdf.kernel.pdf.tagging.IStructureNode;
import com.itextpdf.kernel.pdf.tagging.PdfStructElem;
import com.itextpdf.layout.Canvas;
import com.itextpdf.layout.element.Div;
import com.itextpdf.layout.element.Paragraph;
import com.itextpdf.pdfua.PdfUAConfig;
import com.itextpdf.pdfua.PdfUADocument;

import java.io.IOException;
import java.io.Reader;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

public final class Author {
    private static final Path FONT = Path.of("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf");

    private Author() {
    }

    public static void main(String[] arguments) throws Exception {
        if (arguments.length == 2 && arguments[0].equals("--inspect")) {
            System.out.println(new Gson().toJson(inspect(Path.of(arguments[1]))));
            return;
        }
        if (arguments.length != 3) {
            throw new IllegalArgumentException(
                    "usage: pdf-author CONTRACT SOURCE OUTPUT | pdf-author --inspect OUTPUT");
        }
        JsonObject contract = readContract(Path.of(arguments[0]));
        author(contract, Path.of(arguments[1]), Path.of(arguments[2]));
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

    private static void addSemanticLayer(
            com.itextpdf.kernel.pdf.PdfPage page, PdfFont font, JsonArray nodes) {
        try (Canvas canvas = new Canvas(page, page.getPageSize())) {
            float top = page.getPageSize().getTop() - 1;
            for (var element : nodes) {
                JsonObject node = element.getAsJsonObject();
                String type = requiredString(node, "type");
                switch (type) {
                    case "heading" -> addInvisibleParagraph(canvas, font, top, node, StandardRoles.H1);
                    case "paragraph" -> addInvisibleParagraph(canvas, font, top, node, StandardRoles.P);
                    case "formula" -> addFormula(canvas, font, top, node);
                    case "figure" -> addFigure(canvas, top, node);
                    default -> throw new IllegalArgumentException("unsupported semantic node: " + type);
                }
                top -= 1;
            }
        }
    }

    private static void addInvisibleParagraph(
            Canvas canvas, PdfFont font, float top, JsonObject node, String role) {
        String text = requiredString(node, "text");
        Paragraph paragraph = new Paragraph(text)
                .setFont(font)
                .setFontSize(1)
                .setFixedPosition(0, top, 1)
                .setOpacity(0f);
        paragraph.getAccessibilityProperties().setRole(role);
        paragraph.getAccessibilityProperties().setActualText(text);
        canvas.add(paragraph);
    }

    private static void addFormula(Canvas canvas, PdfFont font, float top, JsonObject node) {
        Paragraph formula = new Paragraph(requiredString(node, "normalized_math"))
                .setFont(font)
                .setFontSize(1)
                .setFixedPosition(0, top, 1)
                .setOpacity(0f);
        formula.getAccessibilityProperties()
                .setRole(StandardRoles.FORMULA)
                .setActualText(requiredString(node, "normalized_math"))
                .setAlternateDescription(requiredString(node, "spoken_math_alternative"));
        canvas.add(formula);
    }

    private static void addFigure(Canvas canvas, float top, JsonObject node) {
        Div figure = new Div().setFixedPosition(0, top, 1).setHeight(1).setOpacity(0f);
        figure.getAccessibilityProperties()
                .setRole(StandardRoles.FIGURE)
                .setAlternateDescription(requiredString(node, "figure_alternative"))
                .setActualText(requiredString(node, "detailed_figure_description"));
        canvas.add(figure);
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
