package com.dicacros.picture;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.TextView;

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
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 네이버 블로그 글쓰기 자동화.
 *
 *  - macdcross / dicajohn / 임의 아이디 로그인 (쿠키 스냅샷으로 다계정 세션 유지 — nfriendcl 방식)
 *  - adsensefarm.kr/realtime 의 다음·구글·크리에이터 어드바이저 실시간 검색어를 WebView DOM 에서 추출
 *  - Gemini / ChatGPT API 로 상세 지침에 맞춘 블로그 초안 생성 + 후처리 정리
 *  - 하단 스플릿뷰(네이버/ChatGPT), 시크바로 비율 조절
 *  - 문단 사이 사진 업로드 슬롯 삽입, 옵션 저장, 일괄 실행
 *  - WebView 파일 선택 지원 → 네이버 에디터에서 작업한 사진 직접 업로드
 */
public class BlogWriterActivity extends Activity {

    private static final String REALTIME_URL = "https://adsensefarm.kr/realtime";
    private static final String CHATGPT_URL = "https://chatgpt.com/";
    private static final String DESKTOP_UA =
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    + "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
    private static final String PREFS = "picture_blog";
    private static final int REQUEST_FILE_CHOOSER = 2001;
    private static final String DEFAULT_OPENAI_MODEL = "gpt-4.1";
    private static final String DEFAULT_GEMINI_MODEL = "gemini-2.5-flash";

    private final ExecutorService executor = Executors.newSingleThreadExecutor();

    private String currentBlogId = NaverAccounts.IDS[0];

    private EditText customIdInput;
    private EditText openAiKeyInput;
    private EditText geminiKeyInput;
    private EditText openAiModelInput;
    private EditText geminiModelInput;
    private EditText topicInput;
    private EditText draftInput;
    private EditText resultOutput;
    private TextView statusText;
    private TextView accountStatusText;
    private TextView keywordPreview;
    private ProgressBar progressBar;

    private WebView workWeb;
    private WebView chatgptWeb;
    private LinearLayout webSplit;

    private CheckBox optImageSlots;
    private CheckBox optRealtime;
    private CheckBox optRelated;
    private CheckBox srcDaum;
    private CheckBox srcGoogle;
    private CheckBox srcCreator;

    private final List<String> collectedKeywords = new ArrayList<>();
    private boolean pendingKeywordExtract = false;
    private boolean generateAfterKeywords = false;
    private boolean openWriterAfterGenerate = false;

    private ValueCallback<Uri[]> fileChooserCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        CookieManager.getInstance().setAcceptCookie(true);
        setContentView(createContentView());
        setupWebView(workWeb, true);
        setupWebView(chatgptWeb, false);
        loadSettings();
        NaverAccounts.applyTo(this, currentBlogId, had -> updateAccountLabel(had));
        chatgptWeb.loadUrl(CHATGPT_URL);
        workWeb.loadUrl(REALTIME_URL);
        setStatus(currentBlogId + " 선택됨. 로그인하거나 블로그 초안을 생성하세요.");
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (NaverAccounts.isLoggedIn()) {
            NaverAccounts.saveCurrentFor(this, currentBlogId);
        }
        CookieManager.getInstance().flush();
        saveSettings();
    }

    @Override
    protected void onDestroy() {
        executor.shutdownNow();
        if (workWeb != null) {
            workWeb.destroy();
        }
        if (chatgptWeb != null) {
            chatgptWeb.destroy();
        }
        super.onDestroy();
    }

    // ---------------------------------------------------------------
    //  UI
    // ---------------------------------------------------------------
    private View createContentView() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.rgb(247, 248, 250));

        ScrollView controlsScroll = new ScrollView(this);
        LinearLayout controls = new LinearLayout(this);
        controls.setOrientation(LinearLayout.VERTICAL);
        controls.setPadding(dp(16), dp(16), dp(16), dp(10));
        controlsScroll.addView(controls);
        root.addView(controlsScroll, new LinearLayout.LayoutParams(-1, 0, 7f));

        controls.addView(title("네이버 블로그 글쓰기"));

        LinearLayout accountRow = row();
        accountRow.addView(smallButton(NaverAccounts.IDS[0], v -> selectAccount(NaverAccounts.IDS[0])));
        accountRow.addView(smallButton(NaverAccounts.IDS[1], v -> selectAccount(NaverAccounts.IDS[1])));
        controls.addView(accountRow);

        customIdInput = input("다른 네이버 블로그 아이디 입력");
        controls.addView(customIdInput);
        controls.addView(fullButton("선택한 계정으로 로그인", v -> loginSelected()));

        accountStatusText = new TextView(this);
        accountStatusText.setTextColor(Color.rgb(52, 64, 84));
        accountStatusText.setTextSize(13);
        controls.addView(accountStatusText);

        openAiKeyInput = input("ChatGPT API key");
        geminiKeyInput = input("Gemini API key");
        openAiModelInput = input("ChatGPT 모델 (기본 " + DEFAULT_OPENAI_MODEL + ")");
        geminiModelInput = input("Gemini 모델 (기본 " + DEFAULT_GEMINI_MODEL + ")");
        topicInput = input("주제 또는 시드 키워드 (예: 폰 미래 전망)");
        draftInput = multiInput("여기에 사용자가 복사한 원문(폰 미래 전망 글)을 붙여넣으세요");
        controls.addView(openAiKeyInput);
        controls.addView(geminiKeyInput);
        controls.addView(openAiModelInput);
        controls.addView(geminiModelInput);
        controls.addView(topicInput);
        controls.addView(draftInput);

        controls.addView(label("실시간 검색어 소스"));
        LinearLayout srcRow = row();
        srcDaum = check("다음", true);
        srcGoogle = check("구글", true);
        srcCreator = check("크리에이터", true);
        srcRow.addView(srcDaum);
        srcRow.addView(srcGoogle);
        srcRow.addView(srcCreator);
        controls.addView(srcRow);

        optRealtime = check("실시간 키워드 사용", true);
        optRelated = check("연관 키워드 확장", true);
        optImageSlots = check("문단 사이 사진 업로드 슬롯 넣기", true);
        controls.addView(optRealtime);
        controls.addView(optRelated);
        controls.addView(optImageSlots);

        keywordPreview = new TextView(this);
        keywordPreview.setTextColor(Color.rgb(71, 84, 103));
        keywordPreview.setTextSize(12);
        keywordPreview.setPadding(0, dp(6), 0, dp(6));
        keywordPreview.setText("실시간 키워드가 여기에 표시됩니다.");
        controls.addView(keywordPreview);

        LinearLayout actionRow = row();
        actionRow.addView(smallButton("실시간 키워드", v -> fetchKeywords(false)));
        actionRow.addView(smallButton("블로그 생성", v -> generateBlog(false)));
        actionRow.addView(smallButton("일괄 실행", v -> runBatch()));
        controls.addView(actionRow);

        LinearLayout action2 = row();
        action2.addView(smallButton("네이버 글쓰기 열기", v -> openNaverWriter()));
        action2.addView(smallButton("결과 복사", v -> copyResult()));
        controls.addView(action2);

        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        controls.addView(progressBar, new LinearLayout.LayoutParams(-1, dp(10)));

        statusText = new TextView(this);
        statusText.setTextColor(Color.rgb(52, 64, 84));
        statusText.setTextSize(13);
        controls.addView(statusText);

        resultOutput = multiInput("생성된 블로그 글이 여기에 나타납니다.");
        resultOutput.setMinLines(9);
        controls.addView(resultOutput);

        controls.addView(label("스플릿뷰 크기 (위: 네이버 / 아래: ChatGPT)"));
        SeekBar seekBar = new SeekBar(this);
        seekBar.setMax(100);
        seekBar.setProgress(58);
        seekBar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar bar, int progress, boolean fromUser) {
                resizeSplit(Math.max(20, Math.min(80, progress)));
            }

            @Override
            public void onStartTrackingTouch(SeekBar bar) {
            }

            @Override
            public void onStopTrackingTouch(SeekBar bar) {
            }
        });
        controls.addView(seekBar);

        webSplit = new LinearLayout(this);
        webSplit.setOrientation(LinearLayout.VERTICAL);
        workWeb = new WebView(this);
        chatgptWeb = new WebView(this);
        webSplit.addView(workWeb, new LinearLayout.LayoutParams(-1, 0, 58f));
        webSplit.addView(chatgptWeb, new LinearLayout.LayoutParams(-1, 0, 42f));
        root.addView(webSplit, new LinearLayout.LayoutParams(-1, 0, 5f));

        return root;
    }

    private void setupWebView(WebView webView, boolean desktop) {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setBuiltInZoomControls(true);
        settings.setDisplayZoomControls(false);
        settings.setSupportMultipleWindows(false);
        settings.setAllowFileAccess(true);
        if (desktop) {
            settings.setUserAgentString(DESKTOP_UA);
        }
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                if (url == null) {
                    return;
                }
                if (NaverAccounts.isLoggedIn()) {
                    NaverAccounts.saveCurrentFor(BlogWriterActivity.this, currentBlogId);
                    updateAccountLabel(true);
                }
                if (view == workWeb && pendingKeywordExtract && url.contains("adsensefarm")) {
                    pendingKeywordExtract = false;
                    view.postDelayed(() -> extractKeywordsFromWorkWeb(), 1200);
                }
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView webView, ValueCallback<Uri[]> filePathCallback,
                                             FileChooserParams fileChooserParams) {
                if (fileChooserCallback != null) {
                    fileChooserCallback.onReceiveValue(null);
                }
                fileChooserCallback = filePathCallback;
                try {
                    Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    intent.setType("image/*");
                    intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                    startActivityForResult(Intent.createChooser(intent, "업로드할 사진 선택"), REQUEST_FILE_CHOOSER);
                    return true;
                } catch (Exception e) {
                    fileChooserCallback = null;
                    return false;
                }
            }
        });
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_FILE_CHOOSER) {
            Uri[] results = null;
            if (resultCode == RESULT_OK && data != null) {
                results = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
            }
            if (fileChooserCallback != null) {
                fileChooserCallback.onReceiveValue(results);
                fileChooserCallback = null;
            }
        }
    }

    // ---------------------------------------------------------------
    //  계정
    // ---------------------------------------------------------------
    private void selectAccount(String id) {
        switchAccount(id);
        customIdInput.setText(id);
    }

    private void switchAccount(String targetId) {
        if (targetId.equals(currentBlogId)) {
            updateAccountLabel(NaverAccounts.hasSession(this, targetId));
            return;
        }
        if (NaverAccounts.isLoggedIn()) {
            NaverAccounts.saveCurrentFor(this, currentBlogId);
        }
        currentBlogId = targetId;
        NaverAccounts.applyTo(this, targetId, had -> updateAccountLabel(had));
        setStatus(targetId + " 계정으로 전환했습니다.");
    }

    private void updateAccountLabel(boolean hadSession) {
        String state = (hadSession || NaverAccounts.isLoggedIn()) ? "로그인 세션 있음" : "로그인 필요";
        accountStatusText.setText("현재 계정: " + currentBlogId + " (" + state + ")");
    }

    private void loginSelected() {
        String customId = customIdInput.getText().toString().trim().toLowerCase(Locale.ROOT);
        if (!customId.isEmpty() && !customId.equals(currentBlogId)) {
            switchAccount(customId);
        }
        pendingKeywordExtract = false;
        workWeb.loadUrl(NaverAccounts.LOGIN_URL);
        setStatus(currentBlogId + " 로그인 화면을 열었습니다. 로그인 후 글쓰기 열기를 누르세요.");
    }

    private void openNaverWriter() {
        String id = selectedBlogId();
        pendingKeywordExtract = false;
        workWeb.loadUrl("https://blog.naver.com/" + urlEncode(id) + "?Redirect=Write");
        setStatus(id + " 글쓰기 화면. 복사한 초안을 붙여넣고 사진을 업로드하세요.");
    }

    private String selectedBlogId() {
        String customId = customIdInput.getText().toString().trim().toLowerCase(Locale.ROOT);
        if (!customId.isEmpty()) {
            if (!customId.equals(currentBlogId)) {
                switchAccount(customId);
            }
            return customId;
        }
        return currentBlogId;
    }

    // ---------------------------------------------------------------
    //  실시간 키워드 추출 (WebView DOM)
    // ---------------------------------------------------------------
    private void fetchKeywords(boolean thenGenerate) {
        generateAfterKeywords = thenGenerate;
        pendingKeywordExtract = true;
        progressBar.setProgress(15);
        setStatus("adsensefarm 실시간 검색어 페이지를 여는 중...");
        String current = workWeb.getUrl();
        if (current != null && current.contains("adsensefarm")) {
            // 이미 실시간 페이지가 열려 있으면 바로 추출.
            pendingKeywordExtract = false;
            extractKeywordsFromWorkWeb();
        } else {
            workWeb.loadUrl(REALTIME_URL);
        }
    }

    private void extractKeywordsFromWorkWeb() {
        final String js = "(function(){try{var s=[];var push=function(t){t=(t||'').replace(/\\s+/g,' ').trim();"
                + "if(t.length>=2&&t.length<=30&&s.indexOf(t)<0)s.push(t);};"
                + "var els=document.querySelectorAll('a,li,td,span,strong,p,div');"
                + "for(var i=0;i<els.length;i++){var el=els[i];if(el.children&&el.children.length>0)continue;"
                + "push(el.innerText||el.textContent);}return JSON.stringify(s.slice(0,500));}"
                + "catch(e){return JSON.stringify([]);}})();";
        workWeb.evaluateJavascript(js, value -> {
            List<String> parsed = parseKeywordJson(value);
            collectedKeywords.clear();
            Set<String> dedup = new LinkedHashSet<>();
            for (String k : parsed) {
                if (looksLikeKeyword(k)) {
                    dedup.add(k);
                }
                if (dedup.size() >= 30) {
                    break;
                }
            }
            collectedKeywords.addAll(dedup);
            if (collectedKeywords.isEmpty()) {
                keywordPreview.setText("키워드를 찾지 못했습니다. 실시간 페이지 로그인이 필요하면 웹뷰에서 로그인 후 다시 시도하세요.");
                setStatus("실시간 키워드 추출 실패.");
            } else {
                keywordPreview.setText("실시간 키워드 " + collectedKeywords.size() + "개: "
                        + join(collectedKeywords, ", "));
                setStatus("실시간 키워드 " + collectedKeywords.size() + "개 확보.");
            }
            progressBar.setProgress(35);
            if (generateAfterKeywords) {
                generateAfterKeywords = false;
                generateBlog(openWriterAfterGenerate);
            }
        });
    }

    private List<String> parseKeywordJson(String rawValue) {
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

    private boolean looksLikeKeyword(String text) {
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
        // 숫자/기호만이거나 순위표시(1위 등)로만 이루어진 짧은 토큰 배제.
        return !text.matches("^[0-9]+\\s*위?$");
    }

    // ---------------------------------------------------------------
    //  블로그 생성
    // ---------------------------------------------------------------
    private void runBatch() {
        openWriterAfterGenerate = true;
        setStatus("일괄 실행: 키워드 수집 → 초안 생성 → 복사 → 글쓰기 열기");
        if (optRealtime.isChecked()) {
            fetchKeywords(true);
        } else {
            generateBlog(true);
        }
    }

    private void generateBlog(final boolean openWriter) {
        progressBar.setProgress(45);
        setStatus("프롬프트를 준비하고 API를 호출합니다...");
        final List<String> keywords = new ArrayList<>(collectedKeywords);
        final String openAiKey = openAiKeyInput.getText().toString().trim();
        final String geminiKey = geminiKeyInput.getText().toString().trim();
        final String openAiModel = modelOr(openAiModelInput, DEFAULT_OPENAI_MODEL);
        final String geminiModel = modelOr(geminiModelInput, DEFAULT_GEMINI_MODEL);
        final boolean imageSlots = optImageSlots.isChecked();
        final boolean related = optRelated.isChecked();
        final String prompt = buildBlogPrompt(keywords, imageSlots, related);

        executor.execute(() -> {
            try {
                String result;
                if (!openAiKey.isEmpty()) {
                    result = callOpenAi(openAiKey, openAiModel, prompt);
                } else if (!geminiKey.isEmpty()) {
                    result = callGemini(geminiKey, geminiModel, prompt);
                } else {
                    // 키가 없으면 프롬프트를 그대로 넘겨 ChatGPT 스플릿뷰에 붙여넣어 쓸 수 있게 한다.
                    result = prompt;
                }
                String cleaned = postProcess(result, imageSlots);
                runOnUiThread(() -> {
                    resultOutput.setText(cleaned);
                    progressBar.setProgress(100);
                    copyToClipboard(cleaned);
                    if (openWriter) {
                        openWriterAfterGenerate = false;
                        setStatus("초안 생성·복사 완료. 글쓰기 화면을 엽니다.");
                        openNaverWriter();
                    } else {
                        setStatus(openAiKey.isEmpty() && geminiKey.isEmpty()
                                ? "API 키가 없어 프롬프트를 복사했습니다. ChatGPT 스플릿뷰에 붙여넣으세요."
                                : "초안 생성·복사 완료. 글쓰기 열기를 누르세요.");
                    }
                });
            } catch (Exception exception) {
                runOnUiThread(() -> {
                    progressBar.setProgress(0);
                    setStatus("생성 실패: " + exception.getMessage());
                });
            }
        });
    }

    private String modelOr(EditText field, String fallback) {
        String v = field.getText().toString().trim();
        return v.isEmpty() ? fallback : v;
    }

    private String buildBlogPrompt(List<String> keywords, boolean imageSlots, boolean related) {
        String topic = topicInput.getText().toString().trim();
        if (topic.isEmpty()) {
            topic = "폰 미래 전망";
        }
        String baseText = draftInput.getText().toString().trim();
        StringBuilder sb = new StringBuilder();
        sb.append("당신은 SEO에 정통한 전문 블로거입니다. 아래 지침을 100% 지켜 네이버 블로그 글을 작성하세요.\n\n");
        sb.append("주제: ").append(topic).append('\n');
        if (!keywords.isEmpty()) {
            sb.append("참고 실시간 검색어(다음·구글·크리에이터 어드바이저): ")
                    .append(join(keywords, ", ")).append('\n');
            sb.append("이 중 잠깐 보고 마는 일회성 키워드는 버리고, 사람들이 오래 궁금해할 검색 의도가 강한 키워드를 골라 활용하세요.\n");
        }
        if (related) {
            sb.append("고른 키워드에서 사람들이 함께 궁금해할 연관 검색어까지 스스로 확장해 글에 자연스럽게 녹이세요.\n");
        }
        if (!baseText.isEmpty()) {
            sb.append("\n사용자가 붙여넣은 원문(이 내용을 토대로 확장·재구성):\n").append(baseText).append('\n');
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

    private String callOpenAi(String apiKey, String model, String prompt) throws Exception {
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

    private String callGemini(String apiKey, String model, String prompt) throws Exception {
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
    //  후처리 정리
    // ---------------------------------------------------------------
    private String postProcess(String text, boolean imageSlots) {
        if (text == null) {
            return "";
        }
        String out = text.replace("**", "").replace("*", "");
        StringBuilder sb = new StringBuilder();
        for (String line : out.split("\n", -1)) {
            String trimmed = line.trim();
            String low = trimmed.toLowerCase(Locale.ROOT);
            // 출처/소스/참고 링크 줄 제거.
            if (low.startsWith("출처") || low.startsWith("소스") || low.startsWith("source")
                    || low.startsWith("참고:") || low.startsWith("http")) {
                continue;
            }
            sb.append(line).append('\n');
        }
        out = sb.toString();
        // 3줄 이상 연속 빈 줄은 2줄로 정리.
        out = out.replaceAll("\\n{3,}", "\n\n");
        out = out.trim();
        if (imageSlots && !out.contains("[사진")) {
            out = insertImageSlots(out);
        }
        return out;
    }

    /** 후처리 폴백: 구분선/❝ 소제목 앞에 사진 슬롯이 없으면 삽입. */
    private String insertImageSlots(String text) {
        String[] lines = text.split("\n", -1);
        StringBuilder sb = new StringBuilder();
        for (String line : lines) {
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
    private String httpPost(String endpoint, String body, String authHeader) throws Exception {
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

    private String readResponse(HttpURLConnection connection) throws Exception {
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

    // ---------------------------------------------------------------
    //  설정 저장/복원
    // ---------------------------------------------------------------
    private void loadSettings() {
        SharedPreferences p = getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        openAiKeyInput.setText(p.getString("openai_key", ""));
        geminiKeyInput.setText(p.getString("gemini_key", ""));
        openAiModelInput.setText(p.getString("openai_model", ""));
        geminiModelInput.setText(p.getString("gemini_model", ""));
        topicInput.setText(p.getString("topic", ""));
        optRealtime.setChecked(p.getBoolean("opt_realtime", true));
        optRelated.setChecked(p.getBoolean("opt_related", true));
        optImageSlots.setChecked(p.getBoolean("opt_image", true));
        srcDaum.setChecked(p.getBoolean("src_daum", true));
        srcGoogle.setChecked(p.getBoolean("src_google", true));
        srcCreator.setChecked(p.getBoolean("src_creator", true));
        currentBlogId = p.getString("account", NaverAccounts.IDS[0]);
        customIdInput.setText(currentBlogId);
    }

    private void saveSettings() {
        getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putString("openai_key", openAiKeyInput.getText().toString().trim())
                .putString("gemini_key", geminiKeyInput.getText().toString().trim())
                .putString("openai_model", openAiModelInput.getText().toString().trim())
                .putString("gemini_model", geminiModelInput.getText().toString().trim())
                .putString("topic", topicInput.getText().toString().trim())
                .putBoolean("opt_realtime", optRealtime.isChecked())
                .putBoolean("opt_related", optRelated.isChecked())
                .putBoolean("opt_image", optImageSlots.isChecked())
                .putBoolean("src_daum", srcDaum.isChecked())
                .putBoolean("src_google", srcGoogle.isChecked())
                .putBoolean("src_creator", srcCreator.isChecked())
                .putString("account", currentBlogId)
                .apply();
    }

    // ---------------------------------------------------------------
    //  기타
    // ---------------------------------------------------------------
    private void copyResult() {
        copyToClipboard(resultOutput.getText().toString());
        setStatus("결과를 클립보드에 복사했습니다.");
    }

    private void copyToClipboard(String text) {
        ClipboardManager clipboard = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        if (clipboard != null) {
            clipboard.setPrimaryClip(ClipData.newPlainText("blog draft", text));
        }
    }

    private void resizeSplit(int topPercent) {
        LinearLayout.LayoutParams top = (LinearLayout.LayoutParams) workWeb.getLayoutParams();
        LinearLayout.LayoutParams bottom = (LinearLayout.LayoutParams) chatgptWeb.getLayoutParams();
        top.weight = topPercent;
        bottom.weight = 100 - topPercent;
        workWeb.setLayoutParams(top);
        chatgptWeb.setLayoutParams(bottom);
        webSplit.requestLayout();
    }

    private String join(List<String> items, String sep) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < items.size(); i++) {
            if (i > 0) {
                sb.append(sep);
            }
            sb.append(items.get(i));
        }
        return sb.toString();
    }

    private TextView title(String text) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextColor(Color.rgb(16, 24, 40));
        view.setTextSize(24);
        view.setTypeface(null, 1);
        view.setPadding(0, 0, 0, dp(10));
        return view;
    }

    private TextView label(String text) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextColor(Color.rgb(52, 64, 84));
        view.setTextSize(13);
        view.setPadding(0, dp(10), 0, dp(4));
        return view;
    }

    private EditText input(String hint) {
        EditText input = new EditText(this);
        input.setHint(hint);
        input.setSingleLine(true);
        input.setTextSize(14);
        input.setPadding(dp(10), 0, dp(10), 0);
        input.setBackgroundColor(Color.WHITE);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(46));
        params.setMargins(0, dp(4), 0, dp(4));
        input.setLayoutParams(params);
        return input;
    }

    private EditText multiInput(String hint) {
        EditText input = new EditText(this);
        input.setHint(hint);
        input.setTextSize(14);
        input.setMinLines(4);
        input.setGravity(Gravity.TOP | Gravity.START);
        input.setPadding(dp(10), dp(8), dp(10), dp(8));
        input.setBackgroundColor(Color.WHITE);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.setMargins(0, dp(6), 0, dp(6));
        input.setLayoutParams(params);
        return input;
    }

    private CheckBox check(String text, boolean checked) {
        CheckBox box = new CheckBox(this);
        box.setText(text);
        box.setChecked(checked);
        box.setTextColor(Color.rgb(52, 64, 84));
        box.setTextSize(13);
        return box;
    }

    private LinearLayout row() {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(0, dp(4), 0, dp(4));
        return row;
    }

    private Button smallButton(String text, View.OnClickListener listener) {
        Button button = new Button(this);
        button.setAllCaps(false);
        button.setText(text);
        button.setTextSize(13);
        button.setTextColor(Color.WHITE);
        button.setBackgroundColor(Color.rgb(47, 111, 237));
        button.setOnClickListener(listener);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, dp(44), 1f);
        params.setMargins(dp(3), 0, dp(3), 0);
        button.setLayoutParams(params);
        return button;
    }

    private Button fullButton(String text, View.OnClickListener listener) {
        Button button = new Button(this);
        button.setAllCaps(false);
        button.setText(text);
        button.setTextSize(14);
        button.setTextColor(Color.WHITE);
        button.setBackgroundColor(Color.rgb(23, 78, 166));
        button.setOnClickListener(listener);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(46));
        params.setMargins(0, dp(6), 0, dp(6));
        button.setLayoutParams(params);
        return button;
    }

    private String urlEncode(String value) {
        try {
            return URLEncoder.encode(value, "UTF-8");
        } catch (Exception exception) {
            return value;
        }
    }

    private void setStatus(String message) {
        statusText.setText(message);
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }
}
