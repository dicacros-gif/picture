package com.dicacros.picture;

import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * 블로그 초안 생성의 순수 로직(프롬프트 구성 · OpenAI/Gemini 호출 · 후처리 · 키워드 필터).
 * UI 의존이 없어 Activity 와 백그라운드 서비스 양쪽에서 그대로 재사용한다.
 */
final class BlogGenerator {

    /** adsensefarm 실시간 검색어 페이지 DOM 에서 후보 키워드를 뽑는 JS. Activity/서비스 공용. */
    static final String KEYWORD_EXTRACT_JS =
            "(function(){try{var s=[];var push=function(t){t=(t||'').replace(/\\s+/g,' ').trim();"
                    + "if(t.length>=2&&t.length<=30&&s.indexOf(t)<0)s.push(t);};"
                    + "var els=document.querySelectorAll('a,li,td,span,strong,p,div');"
                    + "for(var i=0;i<els.length;i++){var el=els[i];if(el.children&&el.children.length>0)continue;"
                    + "push(el.innerText||el.textContent);}return JSON.stringify(s.slice(0,500));}"
                    + "catch(e){return JSON.stringify([]);}})();";

    private BlogGenerator() {
    }

    // ---------------------------------------------------------------
    //  키워드
    // ---------------------------------------------------------------
    static List<String> parseKeywordJson(String rawValue) {
        List<String> out = new ArrayList<>();
        if (rawValue == null || rawValue.isEmpty() || "null".equals(rawValue)) {
            return out;
        }
        try {
            Object first = new JSONTokener(rawValue).nextValue();
            String inner = (first instanceof String) ? (String) first : rawValue;
            JSONArray arr = new JSONArray(inner);
            for (int i = 0; i < arr.length(); i++) {
                String s = arr.optString(i, "").trim();
                if (!s.isEmpty()) {
                    out.add(s);
                }
            }
        } catch (Exception ignored) {
        }
        return out;
    }

    static List<String> filterKeywords(List<String> raw, int max) {
        Set<String> dedup = new LinkedHashSet<>();
        for (String k : raw) {
            if (looksLikeKeyword(k)) {
                dedup.add(k);
            }
            if (dedup.size() >= max) {
                break;
            }
        }
        return new ArrayList<>(dedup);
    }

    static boolean looksLikeKeyword(String text) {
        if (text == null) {
            return false;
        }
        int len = text.length();
        if (len < 2 || len > 28) {
            return false;
        }
        String lower = text.toLowerCase(Locale.ROOT);
        String[] banned = {"adsense", "login", "menu", "http", "www", "copyright", "cookie",
                "로그인", "회원가입", "광고", "실시간", "검색어", "순위", "더보기", "저작권", "바로가기",
                "고객센터", "이용약관", "개인정보", "뉴스", "전체", "카테고리", "구독", "댓글"};
        for (String b : banned) {
            if (lower.contains(b)) {
                return false;
            }
        }
        if (!text.matches(".*[가-힣a-zA-Z].*")) {
            return false;
        }
        return !text.matches("^[0-9]+\\s*위?$");
    }

    // ---------------------------------------------------------------
    //  프롬프트
    // ---------------------------------------------------------------
    static String buildBlogPrompt(String topic, String baseText, List<String> keywords,
                                  boolean imageSlots, boolean related) {
        if (topic == null || topic.trim().isEmpty()) {
            topic = "폰 미래 전망";
        }
        StringBuilder sb = new StringBuilder();
        sb.append("당신은 SEO에 정통한 전문 블로거입니다. 아래 지침을 100% 지켜 네이버 블로그 글을 작성하세요.\n\n");
        sb.append("주제: ").append(topic.trim()).append('\n');
        if (keywords != null && !keywords.isEmpty()) {
            sb.append("사용자가 선택한 실시간 검색어와 연관 검색어(네이버·다음·구글): ")
                    .append(join(keywords, ", ")).append('\n');
            sb.append("이 중 잠깐 보고 마는 일회성 키워드는 버리고, 사람들이 오래 궁금해할 검색 의도가 강한 키워드를 골라 활용하세요.\n");
        }
        if (related) {
            sb.append("고른 키워드에서 사람들이 함께 궁금해할 연관 검색어까지 스스로 확장해 글에 자연스럽게 녹이세요.\n");
        }
        if (baseText != null && !baseText.trim().isEmpty()) {
            sb.append("\n사용자가 붙여넣은 원문(이 내용을 토대로 확장·재구성):\n").append(baseText.trim()).append('\n');
        }
        sb.append("\n==== 형식 규칙 ====\n");
        sb.append("소스와 출처, 링크 URL은 절대 출력하지 마세요.\n");
        sb.append("마크다운과 HTML을 쓰지 말고 플레인 텍스트만 출력하세요.\n");
        sb.append("별표 기호 * 와 ** 는 절대 넣지 마세요. 쌍따옴표를 강조용으로 쓰지 마세요.\n");
        sb.append("물음표 ? 와 퍼센트 %, S&P500 같은 꼭 필요한 기호와 본문 숫자(2%, 2월 등)는 그대로 쓰세요.\n");
        sb.append("첫 줄에 제목을 쓰세요. 제목에는 쉼표 없이 물음표를 넣어 궁금증을 유발하고 연관 검색어를 자연스럽게 포함하세요.\n");
        sb.append("제목과 질문 문장만 반말을 써도 되고, 본문은 존댓말 문어체로 작성하세요.\n");
        sb.append("문장은 '~어요. ~지요. 있으니까요. 무슨 말일까요? 않을까요? ~면 흥미로울 겁니다.' 같은 어미로 끝내세요.\n");
        sb.append("소제목은 후킹 문구로 만들고, 왼쪽에 ❝ 를 붙이며, 소제목 앞줄에는 구분선 ────────────── 을 넣으세요.\n");
        sb.append("소제목 앞뒤로 빈 줄 2줄을 넣으세요. 인덱스 숫자나 기호 없이 문자로만 소제목을 쓰세요.\n");
        sb.append("한 문장이 끝나면 줄바꿈하고, 마침표 뒤에는 빈 줄 2줄, 물음표 뒤에는 빈 줄 1줄을 넣으세요.\n");
        sb.append("목록이 필요하면 첫째, 둘째 식으로 쓰고 숫자 인덱스는 쓰지 마세요.\n");
        sb.append("\n==== 내용 규칙 ====\n");
        sb.append("제목 다음 첫 문단은 전체 글을 읽고 싶게 만드는 후킹 질문으로 시작하세요.\n");
        sb.append("역사적 배경을 설명하고, 고전이나 베스트셀러의 오래된 구절을 인용해 현 상황에 빗대세요.\n");
        sb.append("뜻·의미·정의, 이유·원인, 방법·팁, 시사점, 전략, 미래 전망 중 논리적으로 필요한 것만 골라 분석하세요.\n");
        sb.append("경험 기반 후기, 전문가 시각, 사례 분석을 넣어 E-E-A-T(경험·전문성·권위·신뢰)를 강화하세요.\n");
        sb.append("어려운 단어는 바로 뒤에 뜻과 정의를 풀어 주세요. 같은 단어 반복 대신 유사어를 쓰세요.\n");
        sb.append("사람들이 많이 검색하는 내용을 질문과 답변 형식으로 더하되, 키워드를 직접 언급하지 말고 간접적으로 살리세요.\n");
        sb.append("결론에는 '결론'이라는 단어를 쓰지 말고, 애매하게 양비론으로 끝내지 말고 한쪽 입장을 분명히 하세요.\n");
        sb.append("전체 4000자 이상으로 문단 사이 공백을 넉넉히 두고 작성하세요.\n");
        sb.append("마지막 줄에는 첫 줄 제목과 다른 유사어로 바꾼 SEO 제목을 한 줄 더 쓰고, 그 제목에는 '뜻과 의미'를 포함하세요.\n");
        sb.append("맨 끝에는 이 글과 관련된 해시태그를 10개 이상, 각 태그 앞에 # 를 붙이고 공백으로 구분해 한 줄로 출력하세요. '태그'라는 단어는 쓰지 마세요.\n");
        if (imageSlots) {
            sb.append("각 소제목 위 구분선 바로 앞 줄에 [사진 업로드 위치] 라고 한 줄 표시해, 작업한 사진을 넣을 자리를 알려 주세요.\n");
        }
        sb.append("지침 자체나 작성 의도는 글에 언급하지 마세요. 본문만 출력하세요.\n");
        return sb.toString();
    }

    // ---------------------------------------------------------------
    //  생성 (프로바이더 자동 선택)
    // ---------------------------------------------------------------
    static String generate(String openAiKey, String openAiModel, String geminiKey, String geminiModel,
                           String prompt) throws Exception {
        if (openAiKey != null && !openAiKey.trim().isEmpty()) {
            return callOpenAi(openAiKey.trim(), openAiModel, prompt);
        }
        if (geminiKey != null && !geminiKey.trim().isEmpty()) {
            return callGemini(geminiKey.trim(), geminiModel, prompt);
        }
        return prompt;
    }

    static String callOpenAi(String apiKey, String model, String prompt) throws Exception {
        JSONObject body = new JSONObject();
        body.put("model", model);
        body.put("input", prompt);
        body.put("temperature", 0.6);
        body.put("max_output_tokens", 8000);
        String json = httpPost("https://api.openai.com/v1/responses", body.toString(), "Bearer " + apiKey);
        JSONObject response = new JSONObject(json);
        if (response.has("output_text")) {
            return response.getString("output_text");
        }
        StringBuilder text = new StringBuilder();
        JSONArray output = response.optJSONArray("output");
        if (output != null) {
            for (int i = 0; i < output.length(); i++) {
                JSONArray content = output.getJSONObject(i).optJSONArray("content");
                if (content == null) {
                    continue;
                }
                for (int j = 0; j < content.length(); j++) {
                    JSONObject part = content.getJSONObject(j);
                    String value = part.optString("text", "");
                    if (!value.isEmpty()) {
                        text.append(value).append('\n');
                    }
                }
            }
        }
        return text.length() > 0 ? text.toString() : json;
    }

    static String callGemini(String apiKey, String model, String prompt) throws Exception {
        JSONObject part = new JSONObject().put("text", prompt);
        JSONObject content = new JSONObject().put("parts", new JSONArray().put(part));
        JSONObject generationConfig = new JSONObject()
                .put("temperature", 0.7)
                .put("maxOutputTokens", 8192);
        JSONObject body = new JSONObject()
                .put("contents", new JSONArray().put(content))
                .put("generationConfig", generationConfig);
        String endpoint = "https://generativelanguage.googleapis.com/v1beta/models/"
                + urlEncode(model) + ":generateContent?key=" + urlEncode(apiKey);
        String json = httpPost(endpoint, body.toString(), null);
        JSONObject response = new JSONObject(json);
        JSONArray candidates = response.optJSONArray("candidates");
        if (candidates == null || candidates.length() == 0) {
            return json;
        }
        JSONObject candidate = candidates.getJSONObject(0);
        JSONObject candidateContent = candidate.optJSONObject("content");
        if (candidateContent == null) {
            return json;
        }
        JSONArray parts = candidateContent.optJSONArray("parts");
        if (parts == null || parts.length() == 0) {
            return json;
        }
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < parts.length(); i++) {
            sb.append(parts.getJSONObject(i).optString("text", ""));
        }
        return sb.length() > 0 ? sb.toString() : json;
    }

    // ---------------------------------------------------------------
    //  후처리
    // ---------------------------------------------------------------
    static String postProcess(String text, boolean imageSlots) {
        if (text == null) {
            return "";
        }
        String out = text.replace("**", "").replace("*", "");
        StringBuilder sb = new StringBuilder();
        for (String line : out.split("\n", -1)) {
            String low = line.trim().toLowerCase(Locale.ROOT);
            if (low.startsWith("출처") || low.startsWith("소스") || low.startsWith("source")
                    || low.startsWith("참고:") || low.startsWith("http")) {
                continue;
            }
            sb.append(line).append('\n');
        }
        out = sb.toString().replaceAll("\\n{3,}", "\n\n").trim();
        if (imageSlots && !out.contains("[사진")) {
            out = insertImageSlots(out);
        }
        return out;
    }

    private static String insertImageSlots(String text) {
        StringBuilder sb = new StringBuilder();
        for (String line : text.split("\n", -1)) {
            String t = line.trim();
            if (t.startsWith("──") || t.startsWith("❝")) {
                sb.append("[사진 업로드 위치]\n\n");
            }
            sb.append(line).append('\n');
        }
        return sb.toString().trim();
    }

    // ---------------------------------------------------------------
    //  HTTP
    // ---------------------------------------------------------------
    static String httpPost(String endpoint, String body, String authHeader) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(endpoint).openConnection();
        connection.setConnectTimeout(30000);
        connection.setReadTimeout(120000);
        connection.setRequestMethod("POST");
        connection.setDoOutput(true);
        connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
        if (authHeader != null) {
            connection.setRequestProperty("Authorization", authHeader);
        }
        try (OutputStream outputStream = connection.getOutputStream()) {
            outputStream.write(body.getBytes(StandardCharsets.UTF_8));
        }
        return readResponse(connection);
    }

    private static String readResponse(HttpURLConnection connection) throws Exception {
        int code = connection.getResponseCode();
        InputStream stream = (code >= 200 && code < 300) ? connection.getInputStream() : connection.getErrorStream();
        StringBuilder builder = new StringBuilder();
        if (stream != null) {
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    builder.append(line).append('\n');
                }
            }
        }
        if (code < 200 || code >= 300) {
            throw new IllegalStateException("HTTP " + code + " " + builder);
        }
        return builder.toString();
    }

    static String join(List<String> items, String sep) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < items.size(); i++) {
            if (i > 0) {
                sb.append(sep);
            }
            sb.append(items.get(i));
        }
        return sb.toString();
    }

    private static String urlEncode(String value) {
        try {
            return URLEncoder.encode(value, "UTF-8");
        } catch (Exception exception) {
            return value;
        }
    }
}
