package com.dicacros.picture;

import android.text.Html;

import org.xmlpull.v1.XmlPullParser;
import org.xmlpull.v1.XmlPullParserFactory;

import java.io.BufferedInputStream;
import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

final class RealtimeKeywordFetcher {

    private static final String DAUM_URL = "https://www.daum.net/";
    private static final String GOOGLE_RSS_URL =
            "https://trends.google.com/trending/rss?geo=KR";
    private static final String USER_AGENT =
            "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 "
                    + "(KHTML, like Gecko) Chrome/126 Mobile Safari/537.36";
    private static final Pattern DAUM_ANCHOR = Pattern.compile(
            "<a\\b[^>]*DA=RT1[^>]*>", Pattern.CASE_INSENSITIVE);
    private static final Pattern DAUM_KEYWORD = Pattern.compile(
            "data-tiara-copy\\s*=\\s*[\"']([^\"']+)[\"']",
            Pattern.CASE_INSENSITIVE);
    private static final Pattern DAUM_RANK = Pattern.compile(
            "data-tiara-ordnum\\s*=\\s*[\"'](\\d+)[\"']",
            Pattern.CASE_INSENSITIVE);

    private RealtimeKeywordFetcher() {
    }

    static Result fetch() {
        List<KeywordDatabase.RankedKeyword> rankings = new ArrayList<>();
        List<String> errors = new ArrayList<>();
        int daumCount = 0;
        int googleCount = 0;
        try {
            List<KeywordDatabase.RankedKeyword> daum = fetchDaum();
            rankings.addAll(daum);
            daumCount = daum.size();
        } catch (Exception exception) {
            errors.add("다음: " + conciseMessage(exception));
        }
        try {
            List<KeywordDatabase.RankedKeyword> google = fetchGoogle();
            rankings.addAll(google);
            googleCount = google.size();
        } catch (Exception exception) {
            errors.add("구글: " + conciseMessage(exception));
        }
        return new Result(rankings, daumCount, googleCount, errors);
    }

    private static List<KeywordDatabase.RankedKeyword> fetchDaum() throws Exception {
        String html = readText(DAUM_URL);
        List<KeywordDatabase.RankedKeyword> result = new ArrayList<>();
        Set<String> seen = new LinkedHashSet<>();
        Matcher anchorMatcher = DAUM_ANCHOR.matcher(html);
        while (anchorMatcher.find() && result.size() < 10) {
            String anchor = anchorMatcher.group();
            Matcher keywordMatcher = DAUM_KEYWORD.matcher(anchor);
            Matcher rankMatcher = DAUM_RANK.matcher(anchor);
            if (!keywordMatcher.find() || !rankMatcher.find()) {
                continue;
            }
            String keyword = decodeHtml(keywordMatcher.group(1));
            String key = keyword.toLowerCase(Locale.ROOT);
            if (!KeywordDatabase.isUsableKeyword(keyword) || !seen.add(key)) {
                continue;
            }
            int rank;
            try {
                rank = Integer.parseInt(rankMatcher.group(1));
            } catch (NumberFormatException ignored) {
                rank = result.size() + 1;
            }
            result.add(new KeywordDatabase.RankedKeyword(keyword, "다음", rank));
        }
        if (result.isEmpty()) {
            throw new IllegalStateException("실시간 트렌드 영역을 찾지 못했습니다.");
        }
        return result;
    }

    private static List<KeywordDatabase.RankedKeyword> fetchGoogle() throws Exception {
        HttpURLConnection connection = open(GOOGLE_RSS_URL);
        try (InputStream stream = new BufferedInputStream(connection.getInputStream())) {
            XmlPullParserFactory factory = XmlPullParserFactory.newInstance();
            factory.setNamespaceAware(true);
            XmlPullParser parser = factory.newPullParser();
            parser.setInput(stream, StandardCharsets.UTF_8.name());

            List<KeywordDatabase.RankedKeyword> result = new ArrayList<>();
            Set<String> seen = new LinkedHashSet<>();
            boolean insideItem = false;
            int event = parser.getEventType();
            while (event != XmlPullParser.END_DOCUMENT && result.size() < 10) {
                if (event == XmlPullParser.START_TAG) {
                    String name = parser.getName();
                    if ("item".equalsIgnoreCase(name)) {
                        insideItem = true;
                    } else if (insideItem && "title".equalsIgnoreCase(name)) {
                        String keyword = KeywordDatabase.normalizeKeyword(parser.nextText());
                        String key = keyword.toLowerCase(Locale.ROOT);
                        if (KeywordDatabase.isUsableKeyword(keyword) && seen.add(key)) {
                            result.add(new KeywordDatabase.RankedKeyword(
                                    keyword, "구글", result.size() + 1));
                        }
                    }
                } else if (event == XmlPullParser.END_TAG
                        && "item".equalsIgnoreCase(parser.getName())) {
                    insideItem = false;
                }
                event = parser.next();
            }
            if (result.isEmpty()) {
                throw new IllegalStateException("Google Trends RSS가 비어 있습니다.");
            }
            return result;
        } finally {
            connection.disconnect();
        }
    }

    private static String readText(String url) throws Exception {
        HttpURLConnection connection = open(url);
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(
                connection.getInputStream(), StandardCharsets.UTF_8))) {
            StringBuilder output = new StringBuilder(512 * 1024);
            char[] buffer = new char[8192];
            int read;
            while ((read = reader.read(buffer)) >= 0) {
                output.append(buffer, 0, read);
                if (output.length() > 6 * 1024 * 1024) {
                    throw new IllegalStateException("다음 페이지 응답이 너무 큽니다.");
                }
            }
            return output.toString();
        } finally {
            connection.disconnect();
        }
    }

    private static HttpURLConnection open(String url) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setConnectTimeout(15_000);
        connection.setReadTimeout(20_000);
        connection.setInstanceFollowRedirects(true);
        connection.setRequestProperty("User-Agent", USER_AGENT);
        connection.setRequestProperty("Accept-Language", "ko-KR,ko;q=0.9,en;q=0.7");
        connection.setRequestProperty("Accept", "*/*");
        int code = connection.getResponseCode();
        if (code < 200 || code >= 300) {
            connection.disconnect();
            throw new IllegalStateException("HTTP " + code);
        }
        return connection;
    }

    private static String decodeHtml(String value) {
        return KeywordDatabase.normalizeKeyword(
                Html.fromHtml(value, Html.FROM_HTML_MODE_LEGACY).toString());
    }

    private static String conciseMessage(Exception exception) {
        String message = exception.getMessage();
        if (message == null || message.trim().isEmpty()) {
            return exception.getClass().getSimpleName();
        }
        return message.trim();
    }

    static final class Result {
        final List<KeywordDatabase.RankedKeyword> rankings;
        final int daumCount;
        final int googleCount;
        final List<String> errors;

        Result(List<KeywordDatabase.RankedKeyword> rankings, int daumCount,
               int googleCount, List<String> errors) {
            this.rankings = rankings;
            this.daumCount = daumCount;
            this.googleCount = googleCount;
            this.errors = errors;
        }

        int rawCount() {
            return daumCount + googleCount;
        }
    }
}
