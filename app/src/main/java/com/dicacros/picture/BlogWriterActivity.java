package com.dicacros.picture;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.text.InputType;
import android.text.TextUtils;
import android.view.Gravity;
import android.view.MotionEvent;
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
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 네이버 블로그 글쓰기 자동화 콘솔.
 *
 *  - macdcross/dicajohn/임의 아이디 다계정 로그인(쿠키 스냅샷)
 *  - adsensefarm 실시간 검색어 추출 → Gemini/ChatGPT 로 지침 기반 초안 생성(BlogGenerator)
 *  - 발행 방식: 초안만 / 앱 내 WebView 자동 발행 / 네이버 앱 스플릿뷰 + 접근성 자동 탭
 *  - API 기반 화면 꺼짐·백그라운드 주기 처리(BlogAutoService + AlarmManager)
 *  - 모든 옵션은 AutoConfig 에 저장되어 앱을 껏다 켜도, 재부팅해도 유지
 */
public class BlogWriterActivity extends Activity {

    private static final String CHATGPT_URL = "https://chatgpt.com/";
    private static final String DESKTOP_UA =
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    + "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
    private static final int REQUEST_FILE_CHOOSER = 2001;
    private static final int REQUEST_NOTIFICATIONS = 2002;
    private static final long CHATGPT_TIMEOUT_MS = 180_000L;
    private static final long CHATGPT_POLL_MS = 2500L;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private String currentBlogId = NaverAccounts.IDS[0];
    private String currentGenerationKeyword = "";
    private String currentGenerationFocus = "";

    private EditText customIdInput;
    private EditText openAiKeyInput;
    private EditText geminiKeyInput;
    private EditText openAiModelInput;
    private EditText geminiModelInput;
    private EditText draftInput;
    private EditText intervalInput;
    private EditText resultOutput;
    private TextView statusText;
    private TextView accountStatusText;
    private TextView autoStatusText;
    private ProgressBar progressBar;

    private WebView workWeb;
    private WebView chatgptWeb;
    private LinearLayout webSplit;
    private ScrollView controlsScroll;
    private View splitDivider;
    private View controlsDivider;

    private Button accBtn0;
    private Button accBtn1;
    private Button targetDraftBtn;
    private Button targetWebBtn;
    private Button targetAppBtn;
    private Button winNaverBtn;
    private Button winBothBtn;
    private Button winChatBtn;
    private String windowMode = "both";

    private CheckBox optImageSlots;
    private CheckBox autoPublishCheck;
    private CheckBox useChatGptWebCheck;
    private boolean pendingWebPublish = false;
    private boolean autoImageUploadRequested = false;
    private int chatGenerationToken;
    private int chatAssistantBaseline;
    private int chatStablePolls;
    private long chatGenerationDeadline;
    private String lastChatCandidate = "";

    private ValueCallback<Uri[]> fileChooserCallback;
    private KeywordDatabase keywordDatabase;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        CookieManager.getInstance().setAcceptCookie(true);
        keywordDatabase = new KeywordDatabase(this);
        setContentView(createContentView());
        setupWebView(workWeb, true);
        setupWebView(chatgptWeb, false);
        loadSettings();
        applyWindowMode();
        refreshAllToggles();
        requestNotificationsIfNeeded();
        NaverAccounts.applyTo(this, currentBlogId, had -> updateAccountLabel(had));
        chatgptWeb.loadUrl(AutoConfig.chatGptUrl(this));
        workWeb.loadUrl("https://blog.naver.com/" + urlEncode(currentBlogId));
        updateAutoStatus();
        setStatus(currentBlogId + " 선택됨. 로그인하거나 초안을 생성하세요.");
    }

    @Override
    protected void onResume() {
        super.onResume();
        updateAutoStatus();
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (NaverAccounts.isLoggedIn()) {
            NaverAccounts.saveCurrentFor(this, currentBlogId);
        }
        CookieManager.getInstance().flush();
        rememberCurrentChatGptUrl();
        saveSettings();
    }

    @Override
    protected void onDestroy() {
        chatGenerationToken++;
        executor.shutdownNow();
        if (workWeb != null) {
            workWeb.destroy();
        }
        if (chatgptWeb != null) {
            chatgptWeb.destroy();
        }
        if (keywordDatabase != null) {
            keywordDatabase.close();
        }
        super.onDestroy();
    }

    // ---------------------------------------------------------------
    //  UI
    // ---------------------------------------------------------------
    private View createContentView() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(UiKit.BACKGROUND);
        // 저장된 네이버 아이디가 API 키·모델 칸까지 자동으로 채워지지 않도록 자동완성 차단.
        if (Build.VERSION.SDK_INT >= 26) {
            root.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS);
        }

        controlsScroll = new ScrollView(this);
        controlsScroll.setFillViewport(true);
        controlsScroll.setBackgroundColor(UiKit.BACKGROUND);
        LinearLayout controls = UiKit.screen(this);
        controlsScroll.addView(controls);
        root.addView(controlsScroll, new LinearLayout.LayoutParams(-1, 0, 7f));

        controls.addView(UiKit.backBar(this, "Picture Cleaner · 블로그"));
        LinearLayout header = UiKit.tintedCard(
                this, UiKit.INFO_SOFT, Color.rgb(191, 219, 254));
        header.addView(UiKit.eyebrow(this, "NAVER BLOG STUDIO"));
        header.addView(UiKit.pageTitle(this, "선택한 주제를\n발행 가능한 글로 만드세요"));
        header.addView(UiKit.body(this,
                "계정과 생성 방식을 정한 뒤 한 번의 실행으로 초안·사진·발행을 연결합니다."));
        controls.addView(header);

        LinearLayout accountCard = UiKit.card(this);
        accountCard.addView(UiKit.badge(this, "1 · ACCOUNT", UiKit.NAVER));
        accountCard.addView(UiKit.sectionTitle(this, "네이버 계정"));
        LinearLayout accountRow = row();
        accBtn0 = smallButton(NaverAccounts.IDS[0], v -> selectAccount(NaverAccounts.IDS[0]));
        accBtn1 = smallButton(NaverAccounts.IDS[1], v -> selectAccount(NaverAccounts.IDS[1]));
        accountRow.addView(accBtn0);
        accountRow.addView(accBtn1);
        accountCard.addView(accountRow);

        customIdInput = input("다른 네이버 블로그 아이디 입력");
        accountCard.addView(customIdInput);
        Button loginButton = fullButton("선택한 계정으로 로그인", v -> loginSelected());
        UiKit.stylePrimary(loginButton, UiKit.NAVER);
        accountCard.addView(loginButton);

        accountStatusText = UiKit.status(this);
        accountCard.addView(accountStatusText);
        controls.addView(accountCard);

        LinearLayout generationCard = UiKit.card(this);
        generationCard.addView(UiKit.badge(this, "2 · GENERATE", UiKit.PRIMARY));
        generationCard.addView(UiKit.sectionTitle(this, "글 생성 방식"));
        useChatGptWebCheck = check(
                "API 키 없이 하단 ChatGPT의 현재 열린 GPT 사용 (화면 켜짐)", true);
        generationCard.addView(useChatGptWebCheck);
        generationCard.addView(UiKit.caption(this,
                "하단에서 로그인 후 'Phone 미래 전망' 등 사용할 GPT를 열고 저장하세요."));
        LinearLayout chatGptRow = row();
        chatGptRow.addView(smallButton("현재 GPT 저장", v -> saveCurrentChatGpt()));
        chatGptRow.addView(smallButton("ChatGPT 홈", v -> openChatGptHome()));
        chatGptRow.addView(smallButton("응답 가져오기", v -> captureLatestChatResponse()));
        generationCard.addView(chatGptRow);

        draftInput = multiInput("글에 추가로 반영할 원문이나 지침을 붙여넣으세요");
        generationCard.addView(draftInput);
        LinearLayout generateRow = row();
        generateRow.addView(smallButton("초안만 생성", v -> generateBlog(GenAfter.NONE)));
        generateRow.addView(accentButton(
                "선택 방식 일괄 실행", UiKit.PRIMARY, v -> runBatch()));
        generationCard.addView(generateRow);
        controls.addView(generationCard);

        LinearLayout publishCard = UiKit.card(this);
        publishCard.addView(UiKit.badge(this, "3 · PUBLISH", UiKit.NAVY));
        publishCard.addView(UiKit.sectionTitle(this, "발행 방식과 사진"));
        optImageSlots = check("1번에서 정리한 사진을 글 중간에 자동 삽입", true);
        autoPublishCheck = check("발행 버튼까지 자동으로 누르기", true);
        publishCard.addView(optImageSlots);
        publishCard.addView(autoPublishCheck);

        LinearLayout targetRow = row();
        targetDraftBtn = smallButton("초안만", v -> setTarget(AutoConfig.TARGET_DRAFT));
        targetWebBtn = smallButton("웹뷰 발행", v -> setTarget(AutoConfig.TARGET_WEBVIEW));
        targetAppBtn = smallButton("네이버앱", v -> setTarget(AutoConfig.TARGET_APP));
        targetRow.addView(targetDraftBtn);
        targetRow.addView(targetWebBtn);
        targetRow.addView(targetAppBtn);
        publishCard.addView(targetRow);

        LinearLayout action2 = row();
        action2.addView(smallButton("네이버 글쓰기 열기", v -> openNaverWriter()));
        action2.addView(smallButton("웹뷰 지금 발행", v -> publishNowWebView()));
        publishCard.addView(action2);

        Button appSplitButton = smallButton(
                "네이버 앱 스플릿뷰로 열기", v -> launchNaverAppSplit());
        appSplitButton.setLayoutParams(new LinearLayout.LayoutParams(-1, dp(48)));
        publishCard.addView(appSplitButton);
        LinearLayout supportRow = row();
        supportRow.addView(smallButton("접근성 설정", v -> openAccessibilitySettings()));
        supportRow.addView(smallButton("생성 결과 복사", v -> copyResult()));
        publishCard.addView(supportRow);
        controls.addView(publishCard);

        LinearLayout automationCard = UiKit.tintedCard(
                this, UiKit.WARNING_SOFT, Color.rgb(253, 230, 138));
        automationCard.addView(UiKit.badge(
                this, "OPTIONAL · SCHEDULE", Color.rgb(180, 83, 9)));
        automationCard.addView(UiKit.sectionTitle(this, "화면 꺼짐·주기 자동화"));
        automationCard.addView(UiKit.caption(this,
                "이 기능에만 OpenAI 또는 Gemini API 키가 필요하며 초안·웹뷰 방식으로 동작합니다."));
        openAiKeyInput = secretInput("OpenAI API key");
        geminiKeyInput = secretInput("Gemini API key");
        openAiModelInput = input("OpenAI 모델 (기본 gpt-4.1)");
        geminiModelInput = input("Gemini 모델 (기본 gemini-2.5-flash)");
        automationCard.addView(openAiKeyInput);
        automationCard.addView(geminiKeyInput);
        automationCard.addView(openAiModelInput);
        automationCard.addView(geminiModelInput);
        LinearLayout autoRow = row();
        intervalInput = input("주기(분), 기본 60");
        intervalInput.setLayoutParams(new LinearLayout.LayoutParams(0, dp(48), 1f));
        autoRow.addView(intervalInput);
        autoRow.addView(fullButtonWeighted("자동화 시작/중지", v -> toggleAutomation()));
        automationCard.addView(autoRow);

        autoStatusText = UiKit.status(this);
        automationCard.addView(autoStatusText);
        controls.addView(automationCard);

        LinearLayout resultCard = UiKit.card(this);
        resultCard.addView(UiKit.sectionTitle(this, "생성 결과"));
        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        UiKit.tintProgress(progressBar, UiKit.PRIMARY);
        resultCard.addView(progressBar, new LinearLayout.LayoutParams(-1, dp(8)));

        statusText = UiKit.status(this);
        resultCard.addView(statusText);

        resultOutput = multiInput("생성된 블로그 글이 여기에 나타납니다.");
        resultOutput.setMinLines(9);
        resultCard.addView(resultOutput);
        controls.addView(resultCard);

        LinearLayout displayCard = UiKit.card(this);
        displayCard.addView(UiKit.sectionTitle(this, "아래 작업 화면"));
        LinearLayout winRow = row();
        winNaverBtn = smallButton("네이버만", v -> setWindowMode("naver"));
        winBothBtn = smallButton("둘 다", v -> setWindowMode("both"));
        winChatBtn = smallButton("ChatGPT만", v -> setWindowMode("chat"));
        winRow.addView(winNaverBtn);
        winRow.addView(winBothBtn);
        winRow.addView(winChatBtn);
        displayCard.addView(winRow);

        LinearLayout ratioRow = row();
        ratioRow.addView(smallButton("네이버 크게", v -> setSplitRatio(72f)));
        ratioRow.addView(smallButton("반반", v -> setSplitRatio(50f)));
        ratioRow.addView(smallButton("ChatGPT 크게", v -> setSplitRatio(28f)));
        displayCard.addView(ratioRow);

        displayCard.addView(UiKit.caption(this,
                "위 버튼이나 가운데 손잡이를 위아래로 끌어 네이버·ChatGPT 높이를 조절하세요."));
        controls.addView(displayCard);

        webSplit = new LinearLayout(this);
        webSplit.setOrientation(LinearLayout.VERTICAL);
        webSplit.setPadding(dp(8), dp(8), dp(8), dp(8));
        webSplit.setBackgroundColor(UiKit.BACKGROUND);
        workWeb = new WebView(this);
        chatgptWeb = new WebView(this);
        workWeb.setBackgroundColor(UiKit.SURFACE);
        chatgptWeb.setBackgroundColor(UiKit.SURFACE);
        splitDivider = createSplitDivider();
        webSplit.addView(workWeb, new LinearLayout.LayoutParams(-1, 0, 58f));
        webSplit.addView(splitDivider);
        webSplit.addView(chatgptWeb, new LinearLayout.LayoutParams(-1, 0, 42f));
        controlsDivider = createControlsDivider();
        root.addView(controlsDivider);
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
                if (view != workWeb) {
                    return;
                }
                if (pendingWebPublish && url.contains("blog.naver.com")) {
                    pendingWebPublish = false;
                    view.postDelayed(() -> runWebPublish(), 2800);
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
                if (autoImageUploadRequested && webView == workWeb) {
                    autoImageUploadRequested = false;
                    List<Uri> images = ProcessedImageStore.todayImages(
                            BlogWriterActivity.this, 12);
                    if (!images.isEmpty()) {
                        filePathCallback.onReceiveValue(
                                images.toArray(new Uri[0]));
                        setStatus("오늘 정리한 사진 " + images.size()
                                + "개를 네이버 에디터에 전달했습니다.");
                        return true;
                    }
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

    private void saveCurrentChatGpt() {
        String url = rememberCurrentChatGptUrl();
        if (url.isEmpty()) {
            setStatus("하단에서 ChatGPT 또는 사용할 GPT를 먼저 여세요.");
            return;
        }
        setStatus("현재 하단 ChatGPT/GPT 주소를 저장했습니다.");
    }

    private String rememberCurrentChatGptUrl() {
        String url = chatgptWeb == null ? "" : chatgptWeb.getUrl();
        if (url != null && (url.startsWith("https://chatgpt.com/")
                || url.startsWith("https://chat.openai.com/"))) {
            String reusableUrl = reusableChatGptUrl(url);
            AutoConfig.setString(this, "chatgpt_url", reusableUrl);
            return reusableUrl;
        }
        return "";
    }

    private String reusableChatGptUrl(String rawUrl) {
        try {
            Uri uri = Uri.parse(rawUrl);
            String path = uri.getPath() == null ? "/" : uri.getPath();
            int conversation = path.indexOf("/c/");
            if (conversation >= 0) {
                path = conversation == 0 ? "/" : path.substring(0, conversation);
            }
            return new Uri.Builder()
                    .scheme("https")
                    .authority("chatgpt.com")
                    .path(path)
                    .build()
                    .toString();
        } catch (Exception exception) {
            return CHATGPT_URL;
        }
    }

    private void openChatGptHome() {
        chatGenerationToken++;
        chatgptWeb.loadUrl(CHATGPT_URL);
        setWindowMode("chat");
        setStatus("하단 ChatGPT 홈을 열었습니다. 로그인 후 사용할 GPT를 선택하세요.");
    }

    // ---------------------------------------------------------------
    //  계정
    // ---------------------------------------------------------------
    private void selectAccount(String id) {
        switchAccount(id);
        customIdInput.setText(id);
        refreshAccountToggles();
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
        AutoConfig.setString(this, "account", targetId);
        NaverAccounts.applyTo(this, targetId, had -> updateAccountLabel(had));
        refreshAccountToggles();
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
        pendingWebPublish = false;
        workWeb.loadUrl(NaverAccounts.LOGIN_URL);
        setStatus(currentBlogId + " 로그인 화면을 열었습니다. 로그인 후 글쓰기를 진행하세요.");
    }

    private void openNaverWriter() {
        String id = selectedBlogId();
        pendingWebPublish = false;
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
    //  생성
    // ---------------------------------------------------------------
    private enum GenAfter { NONE, OPEN_WRITER, WEB_PUBLISH, APP_PUBLISH }

    private void runBatch() {
        saveSettings();
        String target = AutoConfig.publishTarget(this);
        if (AutoConfig.TARGET_DRAFT.equals(target)) {
            setStatus("다음 롱테일 주제로 초안을 생성합니다.");
            generateBlog(GenAfter.NONE);
        } else if (AutoConfig.TARGET_APP.equals(target)) {
            setStatus("다음 롱테일 주제로 생성 후 네이버 앱 자동 입력을 진행합니다.");
            generateBlog(GenAfter.APP_PUBLISH);
        } else {
            setStatus("다음 롱테일 주제로 생성 후 웹뷰 자동 입력을 진행합니다.");
            generateBlog(GenAfter.WEB_PUBLISH);
        }
    }

    private void publishNowWebView() {
        setStatus("2번에서 선택한 다음 키워드로 웹뷰 자동 발행을 진행합니다.");
        generateBlog(GenAfter.WEB_PUBLISH);
    }

    private void generateBlog(final GenAfter after) {
        KeywordDatabase.KeywordBundle bundle = keywordDatabase.nextKeywordBundle();
        if (bundle == null) {
            progressBar.setProgress(0);
            setStatus("2번 '실시간 연관 검색어'에서 자동화할 검색어를 먼저 선택하세요.");
            return;
        }
        progressBar.setProgress(45);
        saveSettings();
        currentGenerationKeyword = bundle.seed;
        currentGenerationFocus = bundle.focus;
        final List<String> keywords = new ArrayList<>(bundle.related);
        if (!keywords.contains(bundle.seed)) {
            keywords.add(0, bundle.seed);
        }
        final String openAiKey = openAiKeyInput.getText().toString().trim();
        final String geminiKey = geminiKeyInput.getText().toString().trim();
        final String openAiModel = AutoConfig.openAiModel(this);
        final String geminiModel = AutoConfig.geminiModel(this);
        final boolean imageSlots = optImageSlots.isChecked();
        final String base = draftInput.getText().toString().trim();
        final String prompt = BlogGenerator.buildBlogPrompt(
                bundle.focus, base, keywords, imageSlots, true);

        if (useChatGptWebCheck.isChecked()) {
            generateWithChatGptWeb(prompt, after, imageSlots);
            return;
        }
        if (openAiKey.isEmpty() && geminiKey.isEmpty()) {
            progressBar.setProgress(0);
            setStatus("API 생성 방식을 선택했습니다. OpenAI 또는 Gemini API 키를 입력하거나 "
                    + "'API 키 없이 하단 ChatGPT 사용'을 켜세요.");
            return;
        }

        setStatus("선택한 롱테일 주제로 API 초안을 생성하고 있습니다.");
        executor.execute(() -> {
            try {
                String raw = BlogGenerator.generate(openAiKey, openAiModel, geminiKey, geminiModel, prompt);
                runOnUiThread(() ->
                        completeGeneration(raw, after, imageSlots, "API"));
            } catch (Exception exception) {
                runOnUiThread(() -> {
                    progressBar.setProgress(0);
                    setStatus("생성 실패: " + exception.getMessage());
                });
            }
        });
    }

    private void generateWithChatGptWeb(
            String prompt, GenAfter after, boolean imageSlots) {
        int token = ++chatGenerationToken;
        chatStablePolls = 0;
        lastChatCandidate = "";
        chatGenerationDeadline = System.currentTimeMillis() + CHATGPT_TIMEOUT_MS;
        setWindowMode("both");
        rememberCurrentChatGptUrl();
        progressBar.setProgress(20);
        setStatus("하단 ChatGPT 입력창을 확인하고 있습니다.");
        prepareChatGptPrompt(token, prompt, after, imageSlots, 0);
    }

    private void prepareChatGptPrompt(
            int token, String prompt, GenAfter after, boolean imageSlots, int attempt) {
        chatgptWeb.evaluateJavascript(ChatGptWebAutomation.STATE_JS, rawState -> {
            if (token != chatGenerationToken) {
                return;
            }
            ChatGptWebAutomation.State state =
                    ChatGptWebAutomation.parseState(rawState);
            if (!state.ready) {
                if (attempt < 12) {
                    setStatus("하단 ChatGPT 화면이 준비되기를 기다리고 있습니다.");
                    chatgptWeb.postDelayed(() -> prepareChatGptPrompt(
                            token, prompt, after, imageSlots, attempt + 1), 1000);
                    return;
                }
                failChatGptGeneration(
                        "하단 ChatGPT에 로그인하고 사용할 GPT를 연 뒤 다시 실행하세요.");
                return;
            }
            chatAssistantBaseline = state.assistantCount;
            chatgptWeb.evaluateJavascript(
                    ChatGptWebAutomation.fillPromptJs(prompt), rawFill -> {
                        if (token != chatGenerationToken) {
                            return;
                        }
                        ChatGptWebAutomation.ActionResult fill =
                                ChatGptWebAutomation.parseAction(rawFill);
                        if (!fill.ok) {
                            failChatGptGeneration("ChatGPT 입력 실패: " + fill.error);
                            return;
                        }
                        progressBar.setProgress(35);
                        setStatus("롱테일 주제와 연관 검색어를 ChatGPT에 넣었습니다.");
                        chatgptWeb.postDelayed(
                                () -> sendChatGptPrompt(token, after, imageSlots), 700);
                    });
        });
    }

    private void sendChatGptPrompt(int token, GenAfter after, boolean imageSlots) {
        if (token != chatGenerationToken) {
            return;
        }
        chatgptWeb.evaluateJavascript(ChatGptWebAutomation.SEND_JS, rawSend -> {
            if (token != chatGenerationToken) {
                return;
            }
            ChatGptWebAutomation.ActionResult send =
                    ChatGptWebAutomation.parseAction(rawSend);
            if (!send.ok) {
                failChatGptGeneration("ChatGPT 전송 실패: " + send.error);
                return;
            }
            progressBar.setProgress(50);
            setStatus("하단 ChatGPT가 글을 작성하고 있습니다.");
            chatgptWeb.postDelayed(
                    () -> pollChatGptResponse(token, after, imageSlots), CHATGPT_POLL_MS);
        });
    }

    private void pollChatGptResponse(int token, GenAfter after, boolean imageSlots) {
        if (token != chatGenerationToken) {
            return;
        }
        if (System.currentTimeMillis() >= chatGenerationDeadline) {
            failChatGptGeneration(
                    "ChatGPT 응답 대기 시간이 지났습니다. 응답이 끝나면 '응답 가져오기'를 누르세요.");
            return;
        }
        chatgptWeb.evaluateJavascript(ChatGptWebAutomation.STATE_JS, rawState -> {
            if (token != chatGenerationToken) {
                return;
            }
            ChatGptWebAutomation.State state =
                    ChatGptWebAutomation.parseState(rawState);
            boolean hasNewResponse = state.assistantCount > chatAssistantBaseline
                    && !state.text.trim().isEmpty();
            if (hasNewResponse) {
                if (state.text.equals(lastChatCandidate)) {
                    chatStablePolls++;
                } else {
                    lastChatCandidate = state.text;
                    chatStablePolls = 0;
                }
                progressBar.setProgress(state.busy ? 70 : 85);
                if (!state.busy && chatStablePolls >= 1) {
                    chatGenerationToken++;
                    completeGeneration(state.text, after, imageSlots, "하단 ChatGPT");
                    return;
                }
            }
            chatgptWeb.postDelayed(
                    () -> pollChatGptResponse(token, after, imageSlots), CHATGPT_POLL_MS);
        });
    }

    private void captureLatestChatResponse() {
        chatgptWeb.evaluateJavascript(ChatGptWebAutomation.STATE_JS, rawState -> {
            ChatGptWebAutomation.State state =
                    ChatGptWebAutomation.parseState(rawState);
            if (state.text.trim().isEmpty()) {
                setStatus("하단 ChatGPT에서 가져올 응답이 없습니다.");
                return;
            }
            chatGenerationToken++;
            completeGeneration(
                    state.text, GenAfter.NONE, optImageSlots.isChecked(), "하단 ChatGPT");
        });
    }

    private void completeGeneration(
            String raw, GenAfter after, boolean imageSlots, String provider) {
        String cleaned = BlogGenerator.postProcess(raw, imageSlots);
        if (cleaned.isEmpty()) {
            progressBar.setProgress(0);
            setStatus(provider + " 응답에서 블로그 본문을 찾지 못했습니다.");
            return;
        }
        resultOutput.setText(cleaned);
        AutoConfig.setString(this, "last_result", cleaned);
        progressBar.setProgress(100);
        copyToClipboard(cleaned);
        if (!currentGenerationKeyword.isEmpty()) {
            keywordDatabase.markUsed(
                    currentGenerationKeyword, currentGenerationFocus);
        }
        if (after == GenAfter.OPEN_WRITER) {
            setStatus(provider + " 초안 생성 완료. 네이버 글쓰기 화면을 엽니다.");
            openNaverWriter();
        } else if (after == GenAfter.WEB_PUBLISH) {
            setStatus(provider + " 초안 생성 완료. 웹뷰 자동 입력을 시작합니다.");
            startWebPublish();
        } else if (after == GenAfter.APP_PUBLISH) {
            setStatus(provider + " 초안 생성 완료. 네이버 앱 자동 입력을 시작합니다.");
            launchNaverAppSplit();
        } else {
            setStatus(provider + " 초안 생성·복사 완료.");
        }
    }

    private void failChatGptGeneration(String message) {
        chatGenerationToken++;
        progressBar.setProgress(0);
        setStatus(message);
    }

    // ---------------------------------------------------------------
    //  웹뷰 자동 발행
    // ---------------------------------------------------------------
    private void startWebPublish() {
        String id = selectedBlogId();
        pendingWebPublish = true;
        workWeb.loadUrl(NaverPublisher.writeUrl(id));
        setStatus("네이버 글쓰기 화면 로딩 후 자동 입력·발행을 시도합니다.");
    }

    private void runWebPublish() {
        String content = BlogGenerator.forPublishing(
                resultOutput.getText().toString());
        if (TextUtils.isEmpty(content)) {
            setStatus("발행할 내용이 없습니다. 먼저 초안을 생성하세요.");
            return;
        }
        String[] tb = NaverPublisher.splitTitleBody(content);
        boolean publish = autoPublishCheck.isChecked();
        NaverPublisher.runFill(workWeb, tb[0], tb[1], r1 -> {
            setStatus("본문 입력 시도: " + r1);
            List<Uri> images = optImageSlots.isChecked()
                    ? ProcessedImageStore.todayImages(this, 12)
                    : java.util.Collections.emptyList();
            if (!images.isEmpty()) {
                autoImageUploadRequested = true;
                NaverPublisher.runOpenImagePicker(workWeb, imageResult -> {
                    if (imageResult == null || imageResult.contains("\"clicked\":null")
                            || imageResult.contains("\"error\"")) {
                        autoImageUploadRequested = false;
                    }
                    setStatus("본문 중간 사진 삽입 시도: " + imageResult);
                    workWeb.postDelayed(() -> finishWebPublish(publish), 7000);
                });
            } else {
                finishWebPublish(publish);
            }
        });
    }

    private void finishWebPublish(boolean publish) {
        if (!publish) {
            setStatus("본문·사진 입력 완료. 발행 버튼은 직접 눌러 주세요.");
            return;
        }
        workWeb.postDelayed(() -> NaverPublisher.runPublish(workWeb, result ->
                setStatus("자동 발행 시도 완료: " + result)), 2500);
    }

    // ---------------------------------------------------------------
    //  네이버 앱 스플릿뷰 + 접근성
    // ---------------------------------------------------------------
    private void launchNaverAppSplit() {
        List<Uri> images = optImageSlots.isChecked()
                ? ProcessedImageStore.todayImages(this, 12)
                : java.util.Collections.emptyList();
        NaverAppLauncher.Result launchResult = NaverAppLauncher.launch(this, images);
        if (!launchResult.launched) {
            toast("네이버 블로그 앱이 설치되어 있지 않습니다.");
            openPlayStore(AutoConfig.NAVER_APP_PACKAGE);
            return;
        }
        if (BlogAutoAccessibilityService.isRunning()) {
            String content = BlogGenerator.forPublishing(
                    resultOutput.getText().toString());
            boolean publish = autoPublishCheck.isChecked();
            workWeb.postDelayed(() -> {
                BlogAutoAccessibilityService svc = BlogAutoAccessibilityService.get();
                if (svc != null) {
                    svc.automateNaverPost(
                            content, publish, launchResult.sharedImages);
                }
            }, 2200);
            setStatus("네이버 앱 스플릿뷰 실행 + 접근성 자동 입력을 진행합니다.");
        } else {
            setStatus("네이버 앱을 스플릿뷰로 열었습니다. 접근성 자동 탭을 쓰려면 접근성 설정을 켜세요.");
            openAccessibilitySettings();
        }
    }

    private void openAccessibilitySettings() {
        try {
            startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS));
            toast("목록에서 'Picture 블로그 자동 입력'을 켜 주세요.");
        } catch (Exception e) {
            toast("접근성 설정을 열 수 없습니다.");
        }
    }

    private void openPlayStore(String pkg) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse("market://details?id=" + pkg)));
        } catch (Exception ignored) {
        }
    }

    // ---------------------------------------------------------------
    //  발행 방식 / 주기 자동화
    // ---------------------------------------------------------------
    private void setTarget(String target) {
        AutoConfig.setString(this, "publish_target", target);
        refreshTargetToggles();
        updateAutoStatus();
        setStatus("발행 방식: " + targetLabel(target));
    }

    private String targetLabel(String t) {
        if (AutoConfig.TARGET_DRAFT.equals(t)) {
            return "초안만";
        }
        if (AutoConfig.TARGET_APP.equals(t)) {
            return "네이버 앱 + 접근성";
        }
        return "앱 내 WebView 자동 발행";
    }

    private void toggleAutomation() {
        saveSettings();
        if (AutoConfig.autoEnabled(this)) {
            AutoConfig.setBool(this, "auto_enabled", false);
            AutoConfig.cancel(this);
            setStatus("주기 자동화를 중지했습니다.");
        } else {
            if (keywordDatabase.selectedCount() == 0) {
                setStatus("2번 '실시간 연관 검색어'에서 자동화할 검색어를 먼저 선택하세요.");
                return;
            }
            if (AutoConfig.TARGET_APP.equals(AutoConfig.publishTarget(this))) {
                setStatus("네이버 앱 접근성 자동 입력은 화면이 켜지고 잠금이 풀린 상태에서만 "
                        + "안정적입니다. 주기 자동화에는 '초안만' 또는 '웹뷰 발행'을 선택하세요.");
                return;
            }
            if (openAiKeyInput.getText().toString().trim().isEmpty()
                    && geminiKeyInput.getText().toString().trim().isEmpty()) {
                setStatus("화면 꺼짐·주기 자동화에는 OpenAI 또는 Gemini API 키가 필요합니다. "
                        + "API 없이 사용할 때는 화면을 켜고 '블로그 생성'이나 '일괄 실행'을 누르세요.");
                return;
            }
            if (AutoConfig.TARGET_WEBVIEW.equals(AutoConfig.publishTarget(this))
                    && !NaverAccounts.hasSession(this, currentBlogId)
                    && !NaverAccounts.isLoggedIn()) {
                setStatus("웹뷰 주기 발행 전에 선택한 네이버 계정으로 한 번 로그인해 주세요.");
                return;
            }
            if (AutoConfig.TARGET_WEBVIEW.equals(AutoConfig.publishTarget(this))
                    && Build.VERSION.SDK_INT >= 23 && !Settings.canDrawOverlays(this)) {
                ensureAutomationPermissions();
                setStatus("'다른 앱 위에 표시'를 허용한 뒤 자동화 시작을 다시 누르세요.");
                return;
            }
            int interval = parseInt(intervalInput.getText().toString(), 60);
            if (interval < 15) {
                interval = 15;
            }
            AutoConfig.setInt(this, "interval_min", interval);
            AutoConfig.setBool(this, "auto_enabled", true);
            ensureAutomationPermissions();
            AutoConfig.scheduleNext(this);
            setStatus(interval + "분마다 API 생성과 선택한 방식의 처리를 예약했습니다.");
        }
        updateAutoStatus();
    }

    private void ensureAutomationPermissions() {
        requestNotificationsIfNeeded();
        // WebView 발행에는 오버레이 권한이 있으면 렌더링이 안정적이다.
        if (AutoConfig.TARGET_WEBVIEW.equals(AutoConfig.publishTarget(this))
                && Build.VERSION.SDK_INT >= 23 && !Settings.canDrawOverlays(this)) {
            try {
                startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:" + getPackageName())));
                toast("백그라운드 발행을 위해 '다른 앱 위에 표시'를 허용해 주세요.");
            } catch (Exception ignored) {
            }
        }
        // 배터리 최적화 제외를 요청해 주기 실행이 끊기지 않게 한다.
        try {
            Intent battery = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:" + getPackageName()));
            startActivity(battery);
        } catch (Exception ignored) {
        }
    }

    private void updateAutoStatus() {
        boolean on = AutoConfig.autoEnabled(this);
        String target = targetLabel(AutoConfig.publishTarget(this));
        int interval = AutoConfig.intervalMinutes(this);
        boolean overlay = Build.VERSION.SDK_INT < 23 || Settings.canDrawOverlays(this);
        String accessibility = BlogAutoAccessibilityService.isRunning() ? "접근성 켜짐" : "접근성 꺼짐";
        boolean hasApi = !AutoConfig.openAiKey(this).isEmpty()
                || !AutoConfig.geminiKey(this).isEmpty();
        autoStatusText.setText("자동화 " + (on ? "켜짐" : "꺼짐")
                + " · " + interval + "분 · " + target
                + " · API " + (hasApi ? "준비됨" : "필요")
                + " · 오버레이 " + (overlay ? "허용" : "미허용")
                + " · " + accessibility);
    }

    private void requestNotificationsIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQUEST_NOTIFICATIONS);
        }
    }

    // ---------------------------------------------------------------
    //  설정 저장/복원 (AutoConfig)
    // ---------------------------------------------------------------
    private void loadSettings() {
        openAiKeyInput.setText(AutoConfig.openAiKey(this));
        geminiKeyInput.setText(AutoConfig.geminiKey(this));
        openAiModelInput.setText(rawModel("openai_model"));
        geminiModelInput.setText(rawModel("gemini_model"));
        draftInput.setText(AutoConfig.draftBase(this));
        useChatGptWebCheck.setChecked(AutoConfig.useChatGptWeb(this));
        optImageSlots.setChecked(AutoConfig.useImageSlots(this));
        autoPublishCheck.setChecked(AutoConfig.autoPublish(this));
        intervalInput.setText(String.valueOf(AutoConfig.intervalMinutes(this)));
        windowMode = AutoConfig.prefs(this).getString("window_mode", "both");
        currentBlogId = AutoConfig.account(this);
        customIdInput.setText(currentBlogId);
        String last = AutoConfig.lastResult(this);
        if (!last.isEmpty()) {
            resultOutput.setText(last);
        }
    }

    private String rawModel(String key) {
        return AutoConfig.prefs(this).getString(key, "");
    }

    private void saveSettings() {
        AutoConfig.setString(this, "openai_key", openAiKeyInput.getText().toString().trim());
        AutoConfig.setString(this, "gemini_key", geminiKeyInput.getText().toString().trim());
        AutoConfig.setString(this, "openai_model", openAiModelInput.getText().toString().trim());
        AutoConfig.setString(this, "gemini_model", geminiModelInput.getText().toString().trim());
        AutoConfig.setString(this, "draft_base", draftInput.getText().toString().trim());
        AutoConfig.setBool(this, "use_chatgpt_web", useChatGptWebCheck.isChecked());
        AutoConfig.setBool(this, "opt_image", optImageSlots.isChecked());
        AutoConfig.setBool(this, "auto_publish", autoPublishCheck.isChecked());
        AutoConfig.setInt(this, "interval_min", parseInt(intervalInput.getText().toString(), 60));
        AutoConfig.setString(this, "account", currentBlogId);
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

    @SuppressWarnings("ClickableViewAccessibility")
    private View createControlsDivider() {
        TextView divider = new TextView(this);
        divider.setText("↕  설정 / 작업 화면 높이 조절");
        divider.setGravity(Gravity.CENTER);
        divider.setTextColor(Color.WHITE);
        divider.setTextSize(12);
        divider.setBackground(UiKit.rounded(UiKit.NAVY, 10, this));
        LinearLayout.LayoutParams dividerParams =
                new LinearLayout.LayoutParams(-1, dp(40));
        dividerParams.setMargins(dp(12), dp(4), dp(12), dp(4));
        divider.setLayoutParams(dividerParams);
        divider.setOnTouchListener(new View.OnTouchListener() {
            float startRawY;
            float startControlsWeight;
            float startWebWeight;

            @Override
            public boolean onTouch(View view, MotionEvent event) {
                switch (event.getActionMasked()) {
                    case MotionEvent.ACTION_DOWN:
                        startRawY = event.getRawY();
                        startControlsWeight =
                                ((LinearLayout.LayoutParams) controlsScroll.getLayoutParams()).weight;
                        startWebWeight =
                                ((LinearLayout.LayoutParams) webSplit.getLayoutParams()).weight;
                        view.getParent().requestDisallowInterceptTouchEvent(true);
                        return true;
                    case MotionEvent.ACTION_MOVE:
                        int height = ((View) view.getParent()).getHeight();
                        if (height > 0) {
                            float total = startControlsWeight + startWebWeight;
                            float delta = (event.getRawY() - startRawY) / height * total;
                            float controlsWeight = Math.max(
                                    2f, Math.min(total - 2f, startControlsWeight + delta));
                            setMainSplitWeights(controlsWeight, total - controlsWeight);
                        }
                        return true;
                    case MotionEvent.ACTION_UP:
                    case MotionEvent.ACTION_CANCEL:
                        view.getParent().requestDisallowInterceptTouchEvent(false);
                        view.performClick();
                        return true;
                    default:
                        return false;
                }
            }
        });
        return divider;
    }

    private void setMainSplitWeights(float controlsWeight, float webWeight) {
        LinearLayout.LayoutParams controlsParams =
                (LinearLayout.LayoutParams) controlsScroll.getLayoutParams();
        LinearLayout.LayoutParams webParams =
                (LinearLayout.LayoutParams) webSplit.getLayoutParams();
        controlsParams.weight = controlsWeight;
        webParams.weight = webWeight;
        controlsScroll.setLayoutParams(controlsParams);
        webSplit.setLayoutParams(webParams);
        ((View) webSplit.getParent()).requestLayout();
    }

    @SuppressWarnings("ClickableViewAccessibility")
    private View createSplitDivider() {
        TextView divider = new TextView(this);
        divider.setText("⬍⬍⬍  손잡이를 끌어 높이 조절  ⬍⬍⬍");
        divider.setGravity(Gravity.CENTER);
        divider.setTextColor(Color.WHITE);
        divider.setTextSize(13);
        divider.setTypeface(null, Typeface.BOLD);
        divider.setClickable(true);
        divider.setFocusable(true);
        divider.setBackgroundColor(Color.rgb(90, 103, 122));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(52));
        params.setMargins(0, dp(6), 0, dp(6));
        divider.setLayoutParams(params);
        divider.setOnTouchListener(new View.OnTouchListener() {
            float startRawY;
            float startTopWeight;

            @Override
            public boolean onTouch(View v, MotionEvent e) {
                switch (e.getActionMasked()) {
                    case MotionEvent.ACTION_DOWN:
                        startRawY = e.getRawY();
                        startTopWeight = ((LinearLayout.LayoutParams) workWeb.getLayoutParams()).weight;
                        v.getParent().requestDisallowInterceptTouchEvent(true);
                        return true;
                    case MotionEvent.ACTION_MOVE:
                        int h = webSplit.getHeight();
                        if (h > 0) {
                            float delta = e.getRawY() - startRawY;
                            float top = startTopWeight + delta / h * 100f;
                            setSplitWeights(Math.max(15f, Math.min(85f, top)));
                        }
                        return true;
                    case MotionEvent.ACTION_UP:
                    case MotionEvent.ACTION_CANCEL:
                        v.getParent().requestDisallowInterceptTouchEvent(false);
                        v.performClick();
                        return true;
                    default:
                        return false;
                }
            }
        });
        return divider;
    }

    private void setSplitWeights(float topWeight) {
        LinearLayout.LayoutParams top = (LinearLayout.LayoutParams) workWeb.getLayoutParams();
        LinearLayout.LayoutParams bottom = (LinearLayout.LayoutParams) chatgptWeb.getLayoutParams();
        top.weight = topWeight;
        bottom.weight = 100f - topWeight;
        workWeb.setLayoutParams(top);
        chatgptWeb.setLayoutParams(bottom);
        webSplit.requestLayout();
    }

    /** 손가락 드래그가 어려운 기기를 위한 확실한 비율 버튼. 필요하면 '둘 다' 모드로 전환. */
    private void setSplitRatio(float topWeight) {
        if (!"both".equals(windowMode)) {
            setWindowMode("both");
        }
        setSplitWeights(topWeight);
        setStatus("네이버 " + Math.round(topWeight) + " : ChatGPT " + Math.round(100 - topWeight) + " 비율로 조절했습니다.");
    }

    // ---------------------------------------------------------------
    //  화면 표시 모드 + 버튼 선택 상태
    // ---------------------------------------------------------------
    private void setWindowMode(String mode) {
        windowMode = mode;
        AutoConfig.setString(this, "window_mode", mode);
        applyWindowMode();
        refreshWindowToggles();
    }

    private void applyWindowMode() {
        boolean both = "both".equals(windowMode);
        boolean naverOnly = "naver".equals(windowMode);
        boolean chatOnly = "chat".equals(windowMode);
        workWeb.setVisibility(chatOnly ? View.GONE : View.VISIBLE);
        chatgptWeb.setVisibility(naverOnly ? View.GONE : View.VISIBLE);
        if (splitDivider != null) {
            splitDivider.setVisibility(both ? View.VISIBLE : View.GONE);
        }
        LinearLayout.LayoutParams tp = (LinearLayout.LayoutParams) workWeb.getLayoutParams();
        LinearLayout.LayoutParams bp = (LinearLayout.LayoutParams) chatgptWeb.getLayoutParams();
        if (naverOnly) {
            tp.weight = 100f;
            bp.weight = 0f;
        } else if (chatOnly) {
            tp.weight = 0f;
            bp.weight = 100f;
        } else {
            tp.weight = 58f;
            bp.weight = 42f;
        }
        workWeb.setLayoutParams(tp);
        chatgptWeb.setLayoutParams(bp);
        webSplit.requestLayout();
    }

    private void refreshAllToggles() {
        refreshAccountToggles();
        refreshTargetToggles();
        refreshWindowToggles();
    }

    private void refreshAccountToggles() {
        styleToggle(accBtn0, currentBlogId.equals(NaverAccounts.IDS[0]));
        styleToggle(accBtn1, currentBlogId.equals(NaverAccounts.IDS[1]));
    }

    private void refreshTargetToggles() {
        String t = AutoConfig.publishTarget(this);
        styleToggle(targetDraftBtn, AutoConfig.TARGET_DRAFT.equals(t));
        styleToggle(targetWebBtn, AutoConfig.TARGET_WEBVIEW.equals(t));
        styleToggle(targetAppBtn, AutoConfig.TARGET_APP.equals(t));
    }

    private void refreshWindowToggles() {
        styleToggle(winNaverBtn, "naver".equals(windowMode));
        styleToggle(winBothBtn, "both".equals(windowMode));
        styleToggle(winChatBtn, "chat".equals(windowMode));
    }

    private void styleToggle(Button b, boolean selected) {
        if (b == null) {
            return;
        }
        if (selected) {
            UiKit.stylePrimary(b, UiKit.TEAL);
        } else {
            UiKit.styleSecondary(b);
        }
    }

    private int parseInt(String s, int def) {
        try {
            return Integer.parseInt(s.trim());
        } catch (Exception e) {
            return def;
        }
    }

    private EditText input(String hint) {
        EditText input = new EditText(this);
        input.setHint(hint);
        input.setSingleLine(true);
        UiKit.styleInput(input, false);
        if (Build.VERSION.SDK_INT >= 26) {
            input.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
        }
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(48));
        params.setMargins(0, dp(4), 0, dp(4));
        input.setLayoutParams(params);
        return input;
    }

    private EditText multiInput(String hint) {
        EditText input = new EditText(this);
        input.setHint(hint);
        input.setMinLines(4);
        input.setGravity(Gravity.TOP | Gravity.START);
        UiKit.styleInput(input, true);
        if (Build.VERSION.SDK_INT >= 26) {
            input.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
        }
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.setMargins(0, dp(6), 0, dp(6));
        input.setLayoutParams(params);
        return input;
    }

    private EditText secretInput(String hint) {
        EditText input = input(hint);
        input.setInputType(InputType.TYPE_CLASS_TEXT
                | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        return input;
    }

    private CheckBox check(String text, boolean checked) {
        CheckBox box = new CheckBox(this);
        box.setText(text);
        box.setChecked(checked);
        UiKit.styleCheck(box);
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
        Button button = UiKit.secondaryButton(this, text);
        button.setOnClickListener(listener);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, dp(48), 1f);
        params.setMargins(dp(3), dp(3), dp(3), dp(3));
        button.setLayoutParams(params);
        return button;
    }

    private Button accentButton(
            String text, int color, View.OnClickListener listener) {
        Button button = UiKit.primaryButton(this, text, color);
        button.setOnClickListener(listener);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, dp(48), 1f);
        params.setMargins(dp(3), dp(3), dp(3), dp(3));
        button.setLayoutParams(params);
        return button;
    }

    private Button fullButton(String text, View.OnClickListener listener) {
        Button button = UiKit.primaryButton(this, text, UiKit.NAVY);
        button.setOnClickListener(listener);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(48));
        params.setMargins(0, dp(6), 0, dp(6));
        button.setLayoutParams(params);
        return button;
    }

    private Button fullButtonWeighted(String text, View.OnClickListener listener) {
        Button button = UiKit.primaryButton(this, text, UiKit.NAVY);
        button.setOnClickListener(listener);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, dp(48), 1.4f);
        params.setMargins(dp(6), 0, 0, 0);
        button.setLayoutParams(params);
        return button;
    }

    private String urlEncode(String value) {
        try {
            return java.net.URLEncoder.encode(value, "UTF-8");
        } catch (Exception exception) {
            return value;
        }
    }

    private void setStatus(String message) {
        statusText.setText(message);
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show();
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }
}
