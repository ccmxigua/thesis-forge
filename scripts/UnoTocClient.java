import com.sun.star.beans.PropertyValue;
import com.sun.star.beans.XPropertySet;
import com.sun.star.bridge.XUnoUrlResolver;
import com.sun.star.comp.helper.Bootstrap;
import com.sun.star.container.XIndexAccess;
import com.sun.star.container.XNameAccess;
import com.sun.star.frame.XComponentLoader;
import com.sun.star.frame.XDesktop;
import com.sun.star.frame.XStorable;
import com.sun.star.lang.XComponent;
import com.sun.star.lang.XMultiServiceFactory;
import com.sun.star.lang.XServiceInfo;
import com.sun.star.text.XBookmarksSupplier;
import com.sun.star.text.XDocumentIndex;
import com.sun.star.text.XDocumentIndexesSupplier;
import com.sun.star.text.XTextContent;
import com.sun.star.uno.UnoRuntime;
import com.sun.star.uno.XComponentContext;
import com.sun.star.util.XRefreshable;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Local-pipe client. Updates one existing ContentIndex and exports a new PDF. */
public class UnoTocClient {
    static final Map<String,Object> log = new LinkedHashMap<>();

    static PropertyValue property(String name, Object value) {
        PropertyValue p = new PropertyValue();
        p.Name = name;
        p.Value = value;
        return p;
    }

    static String quote(String value) {
        StringBuilder b = new StringBuilder("\"");
        for (char c : value.toCharArray()) {
            switch (c) {
                case '\\': b.append("\\\\"); break;
                case '"': b.append("\\\""); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                default:
                    if (c < 32) b.append(String.format("\\u%04x", (int)c));
                    else b.append(c);
            }
        }
        return b.append('"').toString();
    }

    static String json(Object value) {
        if (value == null) return "null";
        if (value instanceof Boolean || value instanceof Number) return value.toString();
        if (value instanceof Map<?,?> m) {
            List<String> parts = new ArrayList<>();
            for (Map.Entry<?,?> e : m.entrySet()) parts.add(quote(e.getKey().toString()) + ":" + json(e.getValue()));
            return "{" + String.join(",", parts) + "}";
        }
        if (value instanceof Iterable<?> list) {
            List<String> parts = new ArrayList<>();
            for (Object v : list) parts.add(json(v));
            return "[" + String.join(",", parts) + "]";
        }
        return quote(value.toString());
    }

    static void stage(String value) {
        log.put("stage", value);
        System.out.println(json(Map.of("stage", value, "utc", Instant.now().toString())));
        System.out.flush();
    }

    static Map<String,String> bookmarks(XComponent doc) throws java.lang.Exception {
        XBookmarksSupplier supplier = UnoRuntime.queryInterface(XBookmarksSupplier.class, doc);
        if (supplier == null) throw new IllegalStateException("model has no bookmark supplier");
        XNameAccess names = supplier.getBookmarks();
        String[] values = names.getElementNames();
        Arrays.sort(values);
        Map<String,String> result = new LinkedHashMap<>();
        for (String name : values) {
            XTextContent bookmark = UnoRuntime.queryInterface(XTextContent.class, names.getByName(name));
            if (bookmark == null) throw new IllegalStateException("bookmark text scope unavailable");
            result.put(name, bookmark.getAnchor().getString());
        }
        return result;
    }

    static String indexText(Object index) {
        XTextContent text = UnoRuntime.queryInterface(XTextContent.class, index);
        if (text == null || text.getAnchor() == null) throw new IllegalStateException("index has no anchor");
        return text.getAnchor().getString();
    }

    static Map<String,Object> product(XComponentContext ctx) throws java.lang.Exception {
        Object provider = ctx.getServiceManager().createInstanceWithContext(
            "com.sun.star.configuration.ConfigurationProvider", ctx);
        XMultiServiceFactory factory = UnoRuntime.queryInterface(XMultiServiceFactory.class, provider);
        Object access = factory.createInstanceWithArguments(
            "com.sun.star.configuration.ConfigurationAccess",
            new Object[]{property("nodepath", "/org.openoffice.Setup/Product")});
        XPropertySet properties = UnoRuntime.queryInterface(XPropertySet.class, access);
        Map<String,Object> result = new LinkedHashMap<>();
        for (String key : List.of("ooName", "ooSetupVersion", "ooSetupVersionAboutBox")) {
            result.put(key, String.valueOf(properties.getPropertyValue(key)));
        }
        return result;
    }

    public static void main(String[] args) throws java.lang.Exception {
        if (args.length != 6) throw new IllegalArgumentException("pipe UNO URL, input copy, NEW PDF, receipt, expected version, input SHA256");
        Path source = Path.of(args[1]).toAbsolutePath();
        Path output = Path.of(args[2]).toAbsolutePath();
        Path receipt = Path.of(args[3]).toAbsolutePath();
        if (!Files.isRegularFile(source) || Files.exists(output) || source.equals(output))
            throw new IllegalArgumentException("input unavailable or output already exists");
        XComponent doc = null;
        XDesktop desktop = null;
        boolean success = false;
        log.put("started_utc", Instant.now().toString());
        log.put("source_copy", source.toString());
        log.put("PDF", output.toString());
        log.put("source_store_called", false);
        log.put("native_Word_WPS_verified", false);
        log.put("submission_ready", false);
        Map<String,String> beforeBookmarks = null;
        try {
            stage("connect to isolated local pipe");
            XComponentContext local = Bootstrap.createInitialComponentContext(null);
            Object resolverObject = local.getServiceManager().createInstanceWithContext(
                "com.sun.star.bridge.UnoUrlResolver", local);
            XUnoUrlResolver resolver = UnoRuntime.queryInterface(XUnoUrlResolver.class, resolverObject);
            XComponentContext remote = UnoRuntime.queryInterface(XComponentContext.class, resolver.resolve(args[0]));
            if (remote == null) throw new IllegalStateException("remote component context unavailable");
            Map<String,Object> version = product(remote);
            log.put("product", version);
            if (!version.get("ooSetupVersionAboutBox").toString().equals(args[4]))
                throw new IllegalStateException("wrong renderer version; source not loaded");
            String inputHash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(source)));
            log.put("input_copy_sha256_before", inputHash);
            if (!inputHash.equals(args[5])) throw new IllegalStateException("input hash mismatch; source not loaded");
            Object desktopObject = remote.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", remote);
            desktop = UnoRuntime.queryInterface(XDesktop.class, desktopObject);
            XComponentLoader loader = UnoRuntime.queryInterface(XComponentLoader.class, desktopObject);
            stage("Java UNO client and renderer version verified");
            doc = loader.loadComponentFromURL(source.toUri().toString(), "_blank", 0,
                new PropertyValue[]{property("Hidden", true), property("Silent", true),
                    property("ReadOnly", false),
                    property("MacroExecutionMode", com.sun.star.document.MacroExecMode.NEVER_EXECUTE),
                    property("UpdateDocMode", com.sun.star.document.UpdateDocMode.NO_UPDATE)});
            if (doc == null) throw new IllegalStateException("synthetic document load returned null");
            beforeBookmarks = bookmarks(doc);
            log.put("bookmarks_before", beforeBookmarks);
            XDocumentIndexesSupplier supplier = UnoRuntime.queryInterface(XDocumentIndexesSupplier.class, doc);
            if (supplier == null) throw new IllegalStateException("no document-index supplier");
            XIndexAccess indexes = supplier.getDocumentIndexes();
            log.put("all_index_count", indexes.getCount());
            List<Object> targets = new ArrayList<>();
            for (int i = 0; i < indexes.getCount(); i++) {
                Object candidate = indexes.getByIndex(i);
                XServiceInfo info = UnoRuntime.queryInterface(XServiceInfo.class, candidate);
                if (info != null && info.supportsService("com.sun.star.text.ContentIndex")) targets.add(candidate);
            }
            log.put("ContentIndex_count", targets.size());
            if (targets.size() != 1) throw new IllegalStateException("existing ContentIndex is absent or ambiguous");
            Object index = targets.get(0);
            XPropertySet indexProperties = UnoRuntime.queryInterface(XPropertySet.class, index);
            log.put("index_name", indexProperties.getPropertyValue("Name").toString());
            log.put("index_level", indexProperties.getPropertyValue("Level"));
            log.put("index_create_from_outline", indexProperties.getPropertyValue("CreateFromOutline"));
            log.put("index_text_before", indexText(index));
            stage("explicit existing ContentIndex update");
            XRefreshable refreshable = UnoRuntime.queryInterface(XRefreshable.class, index);
            if (refreshable != null) {
                log.put("explicit_method", "XRefreshable.refresh");
                refreshable.refresh();
            } else {
                XDocumentIndex legacy = UnoRuntime.queryInterface(XDocumentIndex.class, index);
                if (legacy == null) throw new IllegalStateException("index has no supported update interface");
                log.put("explicit_method", "XDocumentIndex.update");
                legacy.update();
            }
            log.put("explicit_call_returned", true);
            log.put("index_text_after_call", indexText(index));
            stage("export NEW isolated PDF without DOCX store");
            XStorable storable = UnoRuntime.queryInterface(XStorable.class, doc);
            storable.storeToURL(output.toUri().toString(), new PropertyValue[]{
                property("FilterName", "writer_pdf_Export"), property("Overwrite", false)});
            if (!Files.isRegularFile(output) || Files.size(output) == 0) throw new IllegalStateException("PDF export missing");
            log.put("index_text_after_export", indexText(index));
            Map<String,String> afterBookmarks = bookmarks(doc);
            log.put("bookmarks_after", afterBookmarks);
            for (Map.Entry<String,String> item : beforeBookmarks.entrySet()) {
                if (!item.getValue().equals(afterBookmarks.get(item.getKey())))
                    throw new IllegalStateException("original bookmark name/range text changed");
            }
            log.put("PDF_bytes", Files.size(output));
            log.put("status", "explicit_update_and_export_completed_pending_PDF_validation");
            success = true;
            stage("update and export finished; validation still separate");
        } catch (Throwable error) {
            log.put("status", "failed");
            log.put("error", error.toString());
            error.printStackTrace(System.err);
        } finally {
            if (doc != null) {
                try { doc.dispose(); log.put("model_disposed_without_store", true); }
                catch (Throwable e) { log.put("dispose_error", e.toString()); success = false; }
            }
            if (desktop != null) {
                try { log.put("isolated_desktop_terminated", desktop.terminate()); }
                catch (Throwable e) { log.put("terminate_error", e.toString()); }
            }
            log.put("finished_utc", Instant.now().toString());
            String afterHash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(source)));
            log.put("input_copy_sha256_after", afterHash);
            if (!afterHash.equals(args[5])) { log.put("input_copy_changed", true); success = false; }
            if (!success) log.put("status", "failed");
            Files.writeString(receipt, json(log) + "\n", StandardCharsets.UTF_8);
        }
        System.exit(success ? 0 : 1);
    }
}
