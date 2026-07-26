package com.dicacros.picture;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

final class RelatedKeywordFetcher {

    private static final String USER_AGENT =
            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                    + "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36";

    private RelatedKeywordFetcher() {
    }

    static Result fetch(String seed, boolean useNaver, boolean useDaum, boolean useGoogle) {
        Result result = new Result(seed);
        if (useNaver) {
            try {
                result.naver.addAll(fetchNaver(seed));
            } catch (Exception exception) {
                result.errors.add("네이버: " + shortMessage(exception));
            }
        }
        if (useDaum) {
            try {
                result.daum.addAll(fetchDaum(seed));
            } catch (Exception exception) {
                result.errors.add("다음: " + shortMessage(exception));
            }
        }
        if (useGoogle) {
            try {
                result.google.addAll(fetchGoogle(seed));
            } catch (Exception exception) {
                result.errors.add("구글: " + shortMessage(exception));
            }
        }
        return result;
    }

    static List<Result> fetchAll(List<String> rawSeeds, int maxConcurrent) {
        Set<String> unique = new LinkedHashSet<>();
        if (rawSeeds != null) {
            for (String rawSeed : rawSeeds) {
                String seed = KeywordDatabase.normalizeKeyword(rawSeed);
                if (KeywordDatabase.isUsableKeyword(seed)) {
                    unique.add(seed);
                }
            }
        }
        if (unique.isEmpty()) {
            return new ArrayList<>();
        }

        int threadCount = Math.max(1, Math.min(maxConcurrent, unique.size()));
        ExecutorService executor = Executors.newFixedThreadPool(threadCount);
        List<Future<Result>> futures = new ArrayList<>();
        for (String seed : unique) {
            futures.add(executor.submit(() -> fetch(seed, true, true, true)));
        }

        List<Result> output = new ArrayList<>();
        try {
            for (Future<Result> future : futures) {
                try {
                    output.add(future.get());
                } catch (Exception exception) {
                    Result failed = new Result("");
                    failed.errors.add(shortMessage(exception));
                    output.add(failed);
                }
            }
        } finally {
            executor.shutdownNow();
        }
        return output;
    }

    private static List<String> fetchNaver(String seed) throws Exception {
        String endpoint = "https://ac.search.naver.com/nx/ac?q=" + encode(seed)
                + "&con=0&frm=nv&ans=2&r_format=json&r_enc=UTF-8"
                + "&r_unicode=0&t_koreng=1&run=2&rev=4&st=100";
        JSONObject root = new JSONObject(httpGet(endpoint));
        JSONArray items = root.optJSONArray("items");
        JSONArray group = items == null ? null : items.optJSONArray(0);
        List<String> suggestions = new ArrayList<>();
        if (group != null) {
            for (int index = 0; index < group.length(); index++) {
                Object value = group.opt(index);
                if (value instanceof JSONArray) {
                    addSuggestion(suggestions, ((JSONArray) value).optString(0), seed);
                } else {
                    addSuggestion(suggestions, String.valueOf(value), seed);
                }
            }
        }
        return suggestions;
    }

    private static List<String> fetchDaum(String seed) throws Exception {
        String endpoint = "https://suggest.search.daum.net/sushi/pc/get?q=" + encode(seed);
        JSONObject root = new JSONObject(httpGet(endpoint));
        JSONArray subkeys = root.optJSONArray("subkeys");
        List<String> suggestions = new ArrayList<>();
        if (subkeys != null) {
            for (int index = 0; index < subkeys.length(); index++) {
                addSuggestion(suggestions, subkeys.optString(index), seed);
            }
        }
        return suggestions;
    }

    private static List<String> fetchGoogle(String seed) throws Exception {
        String endpoint = "https://suggestqueries.google.com/complete/search?client=firefox"
                + "&hl=ko&q=" + encode(seed);
        JSONArray root = new JSONArray(httpGet(endpoint));
        JSONArray values = root.optJSONArray(1);
        List<String> suggestions = new ArrayList<>();
        if (values != null) {
            for (int index = 0; index < values.length(); index++) {
                addSuggestion(suggestions, values.optString(index), seed);
            }
        }
        return suggestions;
    }

    private static void addSuggestion(List<String> output, String value, String seed) {
        String keyword = value == null ? "" : value.replaceAll("\\s+", " ").trim();
        if (keyword.length() < 2 || keyword.length() > 80 || keyword.equalsIgnoreCase(seed.trim())) {
            return;
        }
        if (!output.contains(keyword)) {
            output.add(keyword);
        }
    }

    private static String httpGet(String endpoint) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(endpoint).openConnection();
        connection.setConnectTimeout(15000);
        connection.setReadTimeout(20000);
        connection.setRequestMethod("GET");
        connection.setRequestProperty("User-Agent", USER_AGENT);
        connection.setRequestProperty("Accept", "application/json,text/plain,*/*");
        connection.setRequestProperty("Accept-Language", "ko-KR,ko;q=0.9,en;q=0.7");
        int code = connection.getResponseCode();
        InputStream stream = code >= 200 && code < 300
                ? connection.getInputStream()
                : connection.getErrorStream();
        StringBuilder body = new StringBuilder();
        if (stream != null) {
            try (BufferedReader reader = new BufferedReader(
                    new InputStreamReader(stream, StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    body.append(line);
                }
            }
        }
        connection.disconnect();
        if (code < 200 || code >= 300) {
            throw new IllegalStateException("HTTP " + code);
        }
        return body.toString();
    }

    private static String encode(String value) {
        try {
            return URLEncoder.encode(value, "UTF-8");
        } catch (Exception exception) {
            return value;
        }
    }

    private static String shortMessage(Exception exception) {
        String message = exception.getMessage();
        if (message == null || message.trim().isEmpty()) {
            return exception.getClass().getSimpleName();
        }
        return message.length() > 80 ? message.substring(0, 80) : message;
    }

    static final class Result {
        final String seed;
        final List<String> naver = new ArrayList<>();
        final List<String> daum = new ArrayList<>();
        final List<String> google = new ArrayList<>();
        final List<String> errors = new ArrayList<>();

        Result(String seed) {
            this.seed = seed;
        }

        List<String> all() {
            Set<String> deduplicated = new LinkedHashSet<>();
            deduplicated.addAll(naver);
            deduplicated.addAll(daum);
            deduplicated.addAll(google);
            return new ArrayList<>(deduplicated);
        }
    }
}
