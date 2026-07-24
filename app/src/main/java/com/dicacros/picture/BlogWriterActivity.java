package com.dicacros.picture;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
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
 * 네이버 블로그 글쓰기 + 완전 자동화 콘솔.
 *
 *  - macdcross/dicajohn/임의 아이디 다계정 로그인(쿠키 스냅샷)
 *  - adsensefarm 실시간 검색어 추출 → Gemini/ChatGPT 로 지침 기반 초안 생성(BlogGenerator)
 *  - 발행 방식: 초안만 / 앱 내 WebView 자동 발행 / 네이버 앱 스플릿뷰 + 접근성 자동 탭
 *  - 화면 꺼짐·백그라운드 1시간 주기 완전 자동화(BlogAutoService + AlarmManager)
 *  - 모든 옵션은 AutoConfig 에 저장되어 앱을 껏다 켜도, 재부팅해도 유지
 */
public class BlogWriterActivity extends Activity {

    private static final String REALTIME_URL = "https://adsensefarm.kr/realtime";
    private static final String CHATGPT_URL = "https://chatgpt.com/";
    private static final String DESKTOP_UA =
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    + "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
    private static final int REQUEST_FILE_CHOOSER = 2001;
    private static final int REQUEST_NOTIFICATIONS = 2002;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();

    private String currentBlogId = NaverAccounts.IDS[0];

    private EditText customIdInput;
    private EditText openAiKeyInput;
    private EditText geminiKeyInput;
    private EditText openAiModelInput;
    private EditText geminiModelInput;
    private EditText topicInput;
    private EditText draftInput;
    private EditText intervalInput;
    private EditText resultOutput;
    private TextView statusText;
    private TextView accountStatusText;
    private TextView keywordPreview;
    private TextView autoStatusText;
    private ProgressBar progressBar;

    private WebView workWeb;
    private WebView chatgptWeb;
    private WebView keywordWeb;
    private LinearLayout webSplit;

    private CheckBox optImageSlots;
    private CheckBox optRealtime;
    private CheckBox optRelated;
    private CheckBox autoPublishCheck;
    private CheckBox srcDaum;
    private CheckBox srcGoogle;
    private CheckBox srcCreator;

    private final List<String> collectedKeywords = new ArrayList<>();
    private boolean pendingKeywordExtract = false;
    private boolean pendingWebPublish = false;
    private Runnable afterKeywords = null;

    private ValueCallback<Uri[]> fileChooserCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        CookieManager.getInstance().setAcceptCookie(true);
        setContentView(createContentView());
        setupWebView(workWeb, true);
        setupWebView(chatgptWeb, false);
        setupKeywordWeb();
        loadSettings();
        requestNotificationsIfNeeded();
        NaverAccounts.applyTo(this, currentBlogId, had -> updateAccountLabel(had));
        chatgptWeb.loadUrl(CHATGPT_URL);
        workWeb.loadUrl("https://blog.naver.com/" + urlEncode(currentBlogId));
        updateAutoStatus();
        setStatus(currentBlogId + " 선택됨. 로그인하거나 초안을 생성하세요.");
        // 앱 실행 시 화면에 보이지 않는 웹뷰로 실시간 키워드만 자동 수집.
        keywordWeb.postDelayed(() -> fetchKeywords(null), 600);
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
        if (keywordWeb != null) {
            keywordWeb.destroy();
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

        controls.addView(title("네이버 블로그 자동화"));

        LinearLayout accountRow = row();
        accountRow.addView(smallButton(NaverAccounts.IDS[0], v -> selectAccount(NaverAccounts.IDS[0])));
        accountRow.addView(smallButton(NaverAccounts.IDS[1], v -> selectAccount(NaverAccounts.IDS[1])));
        controls.addView(accountRow);

        customIdInput = input("다른 네이버 블로그 아이디 입력");
        controls.addView(customIdInput);
        controls.addView(fullButton("선택한 계정으로 로그인", v -> loginSelected()));

        accountStatusText = smallLabel("");
        controls.addView(accountStatusText);

        openAiKeyInput = input("ChatGPT API key");
        geminiKeyInput = input("Gemini API key");
        openAiModelInput = input("ChatGPT 모델 (기본 gpt-4.1)");
        geminiModelInput = input("Gemini 모델 (기본 gemini-2.5-flash)");
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
        optImageSlots = check("문단 사이 사진 업로드 슬롯", true);
        autoPublishCheck = check("발행 버튼까지 자동으로 누르기", true);
        controls.addView(optRealtime);
        controls.addView(optRelated);
        controls.addView(optImageSlots);
        controls.addView(autoPublishCheck);

        controls.addView(label("발행 방식"));
        LinearLayout targetRow = row();
        targetRow.addView(smallButton("초안만", v -> setTarget(AutoConfig.TARGET_DRAFT)));
        targetRow.addView(smallButton("웹뷰 발행", v -> setTarget(AutoConfig.TARGET_WEBVIEW)));
        targetRow.addView(smallButton("네이버앱", v -> setTarget(AutoConfig.TARGET_APP)));
        controls.addView(targetRow);

        keywordPreview = smallLabel("실시간 키워드가 여기에 표시됩니다.");
        controls.addView(keywordPreview);

        LinearLayout actionRow = row();
        actionRow.addView(smallButton("실시간 키워드", v -> fetchKeywords(null)));
        actionRow.addView(smallButton("블로그 생성", v -> generateBlog(GenAfter.NONE)));
        actionRow.addView(smallButton("일괄 실행", v -> runBatch()));
        controls.addView(actionRow);

        LinearLayout action2 = row();
        action2.addView(smallButton("네이버 글쓰기 열기", v -> openNaverWriter()));
        action2.addView(smallButton("웹뷰 지금 발행", v -> publishNowWebView()));
        controls.addView(action2);

        LinearLayout action3 = row();
        action3.addView(smallButton("네이버앱 스플릿뷰", v -> launchNaverAppSplit()));
        action3.addView(smallButton("접근성 설정", v -> openAccessibilitySettings()));
        action3.addView(smallButton("결과 복사", v -> copyResult()));
        controls.addView(action3);

        controls.addView(label("완전 자동화 (화면 꺼짐/백그라운드에서도 주기 실행)"));
        LinearLayout autoRow = row();
        intervalInput = input("주기(분), 기본 60");
        intervalInput.setLayoutParams(new LinearLayout.LayoutParams(0, dp(46), 1f));
        autoRow.addView(intervalInput);
        autoRow.addView(fullButtonWeighted("자동화 시작/중지", v -> toggleAutomation()));
        controls.addView(autoRow);

        autoStatusText = smallLabel("");
        controls.addView(autoStatusText);

        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        controls.addView(progressBar, new LinearLayout.LayoutParams(-1, dp(10)));

        statusText = smallLabel("");
        controls.addView(statusText);

        resultOutput = multiInput("생성된 블로그 글이 여기에 나타납니다.");
        resultOutput.setMinLines(9);
        controls.addView(resultOutput);

        controls.addView(smallLabel("아래 회색 분할선을 위아래로 끌어 화면 비율을 조절하세요. (위: 네이버 / 아래: ChatGPT)"));

        webSplit = new LinearLayout(this);
        webSplit.setOrientation(LinearLayout.VERTICAL);
        workWeb = new WebView(this);
        chatgptWeb = new WebView(this);
        webSplit.addView(workWeb, new LinearLayout.LayoutParams(-1, 0, 58f));
        webSplit.addView(createSplitDivider());
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
        AutoConfig.setString(this, "account", targetId);
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
        pendingWebPublish = false;
        workWeb.loadUrl(NaverAccounts.LOGIN_URL);
        setStatus(currentBlogId + " 로그인 화면을 열었습니다. 로그인 후 글쓰기를 진행하세요.");
    }

    private void openNaverWriter() {
        String id = selectedBlogId();
        pendingKeywordExtract = false;
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
    //  실시간 키워드 (화면에 보이지 않는 전용 웹뷰에서 수집)
    // ---------------------------------------------------------------
    private void setupKeywordWeb() {
        keywordWeb = new WebView(this);
        WebSettings s = keywordWeb.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        keywordWeb.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                if (url != null && pendingKeywordExtract && url.contains("adsensefarm")) {
                    pendingKeywordExtract = false;
                    view.postDelayed(() -> extractKeywords(), 1200);
                }
            }
        });
    }

    private void fetchKeywords(Runnable then) {
        afterKeywords = then;
        progressBar.setProgress(15);
        setStatus("실시간 검색어를 백그라운드로 가져오는 중...");
        String current = keywordWeb.getUrl();
        if (current != null && current.contains("adsensefarm")) {
            pendingKeywordExtract = false;
            extractKeywords();
        } else {
            pendingKeywordExtract = true;
            keywordWeb.loadUrl(REALTIME_URL);
        }
    }

    private void extractKeywords() {
        keywordWeb.evaluateJavascript(BlogGenerator.KEYWORD_EXTRACT_JS, value -> {
            collectedKeywords.clear();
            collectedKeywords.addAll(
                    BlogGenerator.filterKeywords(BlogGenerator.parseKeywordJson(value), 30));
            if (collectedKeywords.isEmpty()) {
                keywordPreview.setText("키워드를 찾지 못했습니다. 실시간 페이지 로그인이 필요하면 웹뷰에서 로그인 후 다시 시도하세요.");
                setStatus("실시간 키워드 추출 실패.");
            } else {
                keywordPreview.setText("실시간 키워드 " + collectedKeywords.size() + "개: "
                        + BlogGenerator.join(collectedKeywords, ", "));
                setStatus("실시간 키워드 " + collectedKeywords.size() + "개 확보.");
            }
            progressBar.setProgress(35);
            Runnable then = afterKeywords;
            afterKeywords = null;
            if (then != null) {
                then.run();
            }
        });
    }

    // ---------------------------------------------------------------
    //  생성
    // ---------------------------------------------------------------
    private enum GenAfter { NONE, OPEN_WRITER, WEB_PUBLISH }

    private void runBatch() {
        setStatus("일괄 실행: 키워드 수집 → 초안 생성 → 복사 → 글쓰기 열기");
        if (optRealtime.isChecked()) {
            fetchKeywords(() -> generateBlog(GenAfter.OPEN_WRITER));
        } else {
            generateBlog(GenAfter.OPEN_WRITER);
        }
    }

    private void publishNowWebView() {
        setStatus("웹뷰 자동 발행: 초안 생성 후 발행까지 시도합니다.");
        if (optRealtime.isChecked() && collectedKeywords.isEmpty()) {
            fetchKeywords(() -> generateBlog(GenAfter.WEB_PUBLISH));
        } else {
            generateBlog(GenAfter.WEB_PUBLISH);
        }
    }

    private void generateBlog(final GenAfter after) {
        progressBar.setProgress(45);
        setStatus("프롬프트를 준비하고 API를 호출합니다...");
        saveSettings();
        final List<String> keywords = new ArrayList<>(collectedKeywords);
        final String openAiKey = openAiKeyInput.getText().toString().trim();
        final String geminiKey = geminiKeyInput.getText().toString().trim();
        final String openAiModel = AutoConfig.openAiModel(this);
        final String geminiModel = AutoConfig.geminiModel(this);
        final boolean imageSlots = optImageSlots.isChecked();
        final boolean related = optRelated.isChecked();
        final String topic = topicInput.getText().toString().trim();
        final String base = draftInput.getText().toString().trim();
        final String prompt = BlogGenerator.buildBlogPrompt(topic, base, keywords, imageSlots, related);

        executor.execute(() -> {
            try {
                String raw = BlogGenerator.generate(openAiKey, openAiModel, geminiKey, geminiModel, prompt);
                String cleaned = BlogGenerator.postProcess(raw, imageSlots);
                runOnUiThread(() -> {
                    resultOutput.setText(cleaned);
                    AutoConfig.setString(this, "last_result", cleaned);
                    progressBar.setProgress(100);
                    copyToClipboard(cleaned);
                    boolean noKey = openAiKey.isEmpty() && geminiKey.isEmpty();
                    if (after == GenAfter.OPEN_WRITER) {
                        setStatus("초안 생성·복사 완료. 글쓰기 화면을 엽니다.");
                        openNaverWriter();
                    } else if (after == GenAfter.WEB_PUBLISH) {
                        setStatus("초안 생성·복사 완료. 웹뷰 자동 발행을 시작합니다.");
                        startWebPublish();
                    } else {
                        setStatus(noKey
                                ? "API 키가 없어 프롬프트를 복사했습니다. ChatGPT 스플릿뷰에 붙여넣으세요."
                                : "초안 생성·복사 완료.");
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

    // ---------------------------------------------------------------
    //  웹뷰 자동 발행
    // ---------------------------------------------------------------
    private void startWebPublish() {
        String id = selectedBlogId();
        pendingWebPublish = true;
        pendingKeywordExtract = false;
        workWeb.loadUrl(NaverPublisher.writeUrl(id));
        setStatus("네이버 글쓰기 화면 로딩 후 자동 입력·발행을 시도합니다.");
    }

    private void runWebPublish() {
        String content = resultOutput.getText().toString();
        if (TextUtils.isEmpty(content)) {
            setStatus("발행할 내용이 없습니다. 먼저 초안을 생성하세요.");
            return;
        }
        String[] tb = NaverPublisher.splitTitleBody(content);
        boolean publish = autoPublishCheck.isChecked();
        NaverPublisher.runFill(workWeb, tb[0], tb[1], r1 -> {
            setStatus("본문 입력 시도: " + r1);
            if (!publish) {
                setStatus("본문 입력 완료. 발행 버튼은 직접 눌러 주세요.");
                return;
            }
            workWeb.postDelayed(() -> NaverPublisher.runPublish(workWeb, r2 ->
                    setStatus("자동 발행 시도 완료: " + r2)), 2500);
        });
    }

    // ---------------------------------------------------------------
    //  네이버 앱 스플릿뷰 + 접근성
    // ---------------------------------------------------------------
    private void launchNaverAppSplit() {
        Intent launch = getPackageManager().getLaunchIntentForPackage(AutoConfig.NAVER_APP_PACKAGE);
        if (launch == null) {
            toast("네이버 블로그 앱이 설치되어 있지 않습니다.");
            openPlayStore(AutoConfig.NAVER_APP_PACKAGE);
            return;
        }
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                | Intent.FLAG_ACTIVITY_LAUNCH_ADJACENT
                | Intent.FLAG_ACTIVITY_MULTIPLE_TASK);
        try {
            startActivity(launch);
        } catch (Exception e) {
            toast("앱 실행 실패: " + e.getMessage());
            return;
        }
        if (BlogAutoAccessibilityService.isRunning()) {
            String content = resultOutput.getText().toString();
            boolean publish = autoPublishCheck.isChecked();
            workWeb.postDelayed(() -> {
                BlogAutoAccessibilityService svc = BlogAutoAccessibilityService.get();
                if (svc != null) {
                    svc.automateNaverPost(content, publish);
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
    //  발행 방식 / 완전 자동화
    // ---------------------------------------------------------------
    private void setTarget(String target) {
        AutoConfig.setString(this, "publish_target", target);
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
            setStatus("완전 자동화를 중지했습니다.");
        } else {
            int interval = parseInt(intervalInput.getText().toString(), 60);
            if (interval < 15) {
                interval = 15;
            }
            AutoConfig.setInt(this, "interval_min", interval);
            AutoConfig.setBool(this, "auto_enabled", true);
            ensureAutomationPermissions();
            AutoConfig.scheduleNext(this);
            setStatus(interval + "분마다 자동 글쓰기·발행을 시작합니다. 화면이 꺼져도 동작합니다.");
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
        autoStatusText.setText("자동화 " + (on ? "켜짐" : "꺼짐")
                + " · " + interval + "분 · " + target
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
        topicInput.setText(AutoConfig.topic(this));
        draftInput.setText(AutoConfig.draftBase(this));
        optRealtime.setChecked(AutoConfig.useRealtime(this));
        optRelated.setChecked(AutoConfig.useRelated(this));
        optImageSlots.setChecked(AutoConfig.useImageSlots(this));
        autoPublishCheck.setChecked(AutoConfig.autoPublish(this));
        srcDaum.setChecked(AutoConfig.prefs(this).getBoolean("src_daum", true));
        srcGoogle.setChecked(AutoConfig.prefs(this).getBoolean("src_google", true));
        srcCreator.setChecked(AutoConfig.prefs(this).getBoolean("src_creator", true));
        intervalInput.setText(String.valueOf(AutoConfig.intervalMinutes(this)));
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
        AutoConfig.setString(this, "topic", topicInput.getText().toString().trim());
        AutoConfig.setString(this, "draft_base", draftInput.getText().toString().trim());
        AutoConfig.setBool(this, "opt_realtime", optRealtime.isChecked());
        AutoConfig.setBool(this, "opt_related", optRelated.isChecked());
        AutoConfig.setBool(this, "opt_image", optImageSlots.isChecked());
        AutoConfig.setBool(this, "auto_publish", autoPublishCheck.isChecked());
        AutoConfig.setBool(this, "src_daum", srcDaum.isChecked());
        AutoConfig.setBool(this, "src_google", srcGoogle.isChecked());
        AutoConfig.setBool(this, "src_creator", srcCreator.isChecked());
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

    private View createSplitDivider() {
        View divider = new View(this);
        divider.setBackgroundColor(Color.rgb(150, 160, 176));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(18));
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
                        return true;
                    case MotionEvent.ACTION_MOVE:
                        int h = webSplit.getHeight();
                        if (h > 0) {
                            float delta = e.getRawY() - startRawY;
                            float top = startTopWeight + delta / h * 100f;
                            setSplitWeights(Math.max(15f, Math.min(85f, top)));
                        }
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

    private int parseInt(String s, int def) {
        try {
            return Integer.parseInt(s.trim());
        } catch (Exception e) {
            return def;
        }
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
        view.setTypeface(null, 1);
        view.setPadding(0, dp(12), 0, dp(4));
        return view;
    }

    private TextView smallLabel(String text) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextColor(Color.rgb(71, 84, 103));
        view.setTextSize(12);
        view.setPadding(0, dp(4), 0, dp(4));
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

    private Button fullButtonWeighted(String text, View.OnClickListener listener) {
        Button button = new Button(this);
        button.setAllCaps(false);
        button.setText(text);
        button.setTextSize(14);
        button.setTextColor(Color.WHITE);
        button.setBackgroundColor(Color.rgb(23, 78, 166));
        button.setOnClickListener(listener);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, dp(46), 1.4f);
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
