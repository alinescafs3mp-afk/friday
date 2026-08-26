// Observe-only bounded function index for Friday Engineer mode.
// The enclosing Python adapter supplies the sole fixed output path and never
// exposes arbitrary Ghidra script or command-line arguments.

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.app.util.headless.HeadlessAnalyzer;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.List;

public class FridayDecompile extends GhidraScript {
    private static final String SCHEMA = "friday.engineer.decompile.v1";
    private static final String OUTPUT_PATH = "/work/ghidra-decompile.json";
    private static final int MAX_FUNCTIONS = 32;
    private static final int MAX_SCANNED_FUNCTIONS = 4096;
    private static final int MAX_FUNCTION_NAME_CHARS = 256;
    private static final int MAX_SIGNATURE_CHARS = 1024;
    private static final int MAX_PSEUDOCODE_CHARS = 6000;
    private static final int MAX_TOTAL_PSEUDOCODE_CHARS = 160000;
    private static final int MAX_JSON_BYTES = 512 * 1024;
    private static final int JSON_FOOTER_RESERVE_BYTES = 8192;
    private static final int DECOMPILE_TIMEOUT_SECONDS = 1;

    private static String bounded(String value, int maximum) {
        if (value == null || value.isEmpty()) {
            return "";
        }
        return value.length() <= maximum ? value : value.substring(0, maximum);
    }

    private static String jsonString(String value) {
        StringBuilder escaped = new StringBuilder(value.length() + 16);
        escaped.append('"');
        for (int index = 0; index < value.length(); index++) {
            char item = value.charAt(index);
            switch (item) {
                case '"':
                    escaped.append("\\\"");
                    break;
                case '\\':
                    escaped.append("\\\\");
                    break;
                case '\b':
                    escaped.append("\\b");
                    break;
                case '\f':
                    escaped.append("\\f");
                    break;
                case '\n':
                    escaped.append("\\n");
                    break;
                case '\r':
                    escaped.append("\\r");
                    break;
                case '\t':
                    escaped.append("\\t");
                    break;
                default:
                    if (item < 0x20 || Character.isSurrogate(item)) {
                        escaped.append(String.format("\\u%04x", (int) item));
                    }
                    else {
                        escaped.append(item);
                    }
            }
        }
        escaped.append('"');
        return escaped.toString();
    }

    private static int utf8Length(String value) {
        return value.getBytes(StandardCharsets.UTF_8).length;
    }

    private static String functionJson(
            Function function,
            String status,
            String pseudocode,
            boolean pseudocodeTruncated) {
        String name = bounded(function.getName(true), MAX_FUNCTION_NAME_CHARS);
        String signature = bounded(function.getSignature().toString(), MAX_SIGNATURE_CHARS);
        StringBuilder item = new StringBuilder(pseudocode.length() + 1400);
        item.append('{');
        item.append("\"address\":").append(jsonString(function.getEntryPoint().toString()));
        item.append(",\"name\":").append(jsonString(name));
        item.append(",\"signature\":").append(jsonString(signature));
        item.append(",\"pseudocode\":").append(jsonString(pseudocode));
        item.append(",\"decompile_status\":").append(jsonString(status));
        item.append(",\"pseudocode_truncated\":").append(pseudocodeTruncated);
        item.append(",\"thunk\":").append(function.isThunk());
        item.append('}');
        return item.toString();
    }

    @Override
    protected void run() throws Exception {
        String[] arguments = getScriptArgs();
        if (arguments.length != 1 || !OUTPUT_PATH.equals(arguments[0])) {
            throw new IllegalArgumentException("output_path_invalid");
        }
        if (currentProgram == null) {
            throw new IllegalStateException("program_missing");
        }

        List<Function> primary = new ArrayList<>(MAX_FUNCTIONS);
        List<Function> thunks = new ArrayList<>(MAX_FUNCTIONS);
        FunctionIterator iterator = currentProgram.getFunctionManager().getFunctions(true);
        int discovered = 0;
        boolean indexTruncated = false;
        while (iterator.hasNext()) {
            monitor.checkCancelled();
            Function function = iterator.next();
            if (function.isExternal()) {
                continue;
            }
            discovered++;
            if (function.isThunk()) {
                if (thunks.size() < MAX_FUNCTIONS) {
                    thunks.add(function);
                }
            }
            else if (primary.size() < MAX_FUNCTIONS) {
                primary.add(function);
            }
            if (discovered >= MAX_SCANNED_FUNCTIONS) {
                indexTruncated = iterator.hasNext();
                break;
            }
        }
        List<Function> selected = new ArrayList<>(MAX_FUNCTIONS);
        selected.addAll(primary.subList(0, Math.min(primary.size(), MAX_FUNCTIONS)));
        int remainingSlots = MAX_FUNCTIONS - selected.size();
        if (remainingSlots > 0) {
            selected.addAll(thunks.subList(0, Math.min(thunks.size(), remainingSlots)));
        }

        DecompInterface decompiler = new DecompInterface();
        decompiler.setOptions(new DecompileOptions());
        decompiler.toggleCCode(true);
        decompiler.toggleSyntaxTree(false);
        boolean opened = decompiler.openProgram(currentProgram);
        StringBuilder functions = new StringBuilder();
        int pseudocodeChars = 0;
        boolean outputTruncated = false;
        int emitted = 0;
        try {
            for (Function function : selected) {
                monitor.checkCancelled();
                String status = "failed";
                String pseudocode = "";
                boolean bodyTruncated = false;
                if (opened) {
                    try {
                        DecompileResults result = decompiler.decompileFunction(
                            function, DECOMPILE_TIMEOUT_SECONDS, monitor);
                        if (result.isTimedOut()) {
                            status = "timeout";
                        }
                        else if (result.decompileCompleted() && result.getDecompiledFunction() != null) {
                            status = "completed";
                            String complete = result.getDecompiledFunction().getC();
                            if (complete != null) {
                                int remaining = Math.max(
                                    0, MAX_TOTAL_PSEUDOCODE_CHARS - pseudocodeChars);
                                int allowed = Math.min(MAX_PSEUDOCODE_CHARS, remaining);
                                pseudocode = bounded(complete, allowed);
                                bodyTruncated = complete.length() > pseudocode.length();
                                pseudocodeChars += pseudocode.length();
                                outputTruncated |= bodyTruncated;
                            }
                        }
                    }
                    catch (RuntimeException decompileFailure) {
                        // Artifact-controlled decompiler failures are represented only
                        // by a fixed status. Their messages never enter the JSON result.
                        status = "failed";
                        pseudocode = "";
                    }
                }
                String item = functionJson(function, status, pseudocode, bodyTruncated);
                int projected = utf8Length(functions.toString()) + utf8Length(item) +
                    JSON_FOOTER_RESERVE_BYTES + (emitted == 0 ? 0 : 1);
                if (projected > MAX_JSON_BYTES) {
                    outputTruncated = true;
                    break;
                }
                if (emitted > 0) {
                    functions.append(',');
                }
                functions.append(item);
                emitted++;
                if (pseudocodeChars >= MAX_TOTAL_PSEUDOCODE_CHARS) {
                    outputTruncated |= selected.size() > emitted;
                    break;
                }
            }
        }
        finally {
            decompiler.dispose();
        }

        boolean analysisTimedOut = HeadlessAnalyzer.getInstance().checkAnalysisTimedOut();
        boolean omittedFunctions = selected.size() > emitted || discovered > emitted;
        indexTruncated |= omittedFunctions;
        StringBuilder document = new StringBuilder(functions.length() + 2048);
        document.append('{');
        document.append("\"schema\":").append(jsonString(SCHEMA));
        document.append(",\"language_id\":")
            .append(jsonString(bounded(currentProgram.getLanguageID().toString(), 160)));
        document.append(",\"compiler_spec_id\":")
            .append(jsonString(bounded(currentProgram.getCompilerSpec().getCompilerSpecID().toString(), 160)));
        document.append(",\"analysis_timed_out\":").append(analysisTimedOut);
        document.append(",\"function_count_lower_bound\":").append(discovered);
        document.append(",\"function_index_truncated\":").append(indexTruncated);
        document.append(",\"pseudocode_chars\":").append(pseudocodeChars);
        document.append(",\"output_truncated\":").append(outputTruncated);
        document.append(",\"functions\": [").append(functions).append("]}");
        byte[] encoded = document.toString().getBytes(StandardCharsets.UTF_8);
        if (encoded.length > MAX_JSON_BYTES) {
            throw new IllegalStateException("output_cap_exceeded");
        }
        Path output = Paths.get(OUTPUT_PATH);
        Files.write(output, encoded, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE);
    }
}
