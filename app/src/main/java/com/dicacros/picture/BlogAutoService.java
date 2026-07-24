package com.dicacros.picture;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.PixelFormat;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.util.DisplayMetrics;
import android.view.Gravity;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 화면이 꺼져 있거나 백그라운드일 때도 설정한 주기(기본 1시간)마다 스스로 글을 쓰고 발행까지 시도하는
 * 포그라운드 서비스. 웨이크락으로 CPU 를 유지하고, 오버레이 권한이 있으면 WebView 를 화면 밖 오버레이로
 * 붙여 SmartEditor 가 렌더링되게 한다.
 *
 * 파이프라인: 계정 쿠키 복원 → (옵션)실시간 키워드 → API 생성 → 후처리 → 발행(WebView/앱/초안).
 */
public class BlogAutoService extends Service {

    static final String EXTRA_REASON = "reason";
    private static final String CHANNEL = "auto_post";
    private static final int NOTI_ID = 4020;
    private static final long HARD_TIMEOUT_MS = 150_000L;
    private static final String REALTIME_URL = "https://adsensefarm.kr/realtime";

    private enum Stage { IDLE, KEYWORDS, GENERATING, PUBLISHING }

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService exec = Executors.newSingleThreadExecutor();

    private WebView web;
    private boolean overlayAttached;
    private PowerManager.WakeLock wakeLock;
    private volatile boolean finished;

    private Stage stage = Stage.IDLE;
    private String account;
    private String publishTarget;
    private boolean autoPublish;
    private boolean useRealtime;
    private boolean useRelated;
    private boolean useImageSlots;
    private String generated = "";
    private List<String> keywords = new ArrayList<>();

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startForegroundSafely("자동 글쓰기를 준비하고 있어요");
        acquireWake();
        account = AutoConfig.account(this);
        publishTarget = AutoConfig.publishTarget(this);
        autoPublish = AutoConfig.autoPublish(this);
        useRealtime = AutoConfig.useRealtime(this);
        useRelated = AutoConfig.useRelated(this);
        useImageSlots = AutoConfig.useImageSlots(this);

        main.postDelayed(this::finishSelf, HARD_TIMEOUT_MS);
        NaverAccounts.applyTo(this, account, had -> onAccountReady());
        return START_NOT_STICKY;
    }

    private void onAccountReady() {
        if (finished) {
            return;
        }
        try {
            web = createWebView();
        } catch (Throwable t) {
            notifyText("WebView 생성 실패: " + t.getMessage());
            finishSelf();
            return;
        }
        if (useRealtime) {
            stage = Stage.KEYWORDS;
            notifyText("실시간 키워드를 수집하고 있어요");
            web.loadUrl(REALTIME_URL);
        } else {
            startGenerate();
        }
    }

    private WebView createWebView() {
        WebView w = new WebView(this);
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(w, true);
        w.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                if (finished || url == null) {
                    return;
                }
                if (stage == Stage.KEYWORDS && url.contains("adsensefarm")) {
                    stage = Stage.GENERATING;
                    main.postDelayed(() -> extractKeywords(), 1500);
                } else if (stage == Stage.PUBLISHING && url.contains("blog.naver.com")) {
                    main.postDelayed(() -> fillThenPublish(), 2800);
                }
            }
        });
        attachOverlay(w);
        return w;
    }

    private void attachOverlay(WebView w) {
        if (!canOverlay()) {
            return;
        }
        try {
            DisplayMetrics dm = getResources().getDisplayMetrics();
            int type = Build.VERSION.SDK_INT >= 26
                    ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                    : WindowManager.LayoutParams.TYPE_PHONE;
            WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                    Math.max(720, dm.widthPixels),
                    Math.max(1280, dm.heightPixels),
                    type,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                            | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                            | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                    PixelFormat.TRANSLUCENT);
            lp.gravity = Gravity.TOP | Gravity.START;
            lp.x = 0;
            lp.y = 0;
            w.setAlpha(0.02f);
            WindowManager wm = (WindowManager) getSystemService(WINDOW_SERVICE);
            if (wm != null) {
                wm.addView(w, lp);
                overlayAttached = true;
            }
        } catch (Throwable ignored) {
        }
    }

    private boolean canOverlay() {
        return Build.VERSION.SDK_INT < 23 || Settings.canDrawOverlays(this);
    }

    private void extractKeywords() {
        if (finished) {
            return;
        }
        web.evaluateJavascript(BlogGenerator.KEYWORD_EXTRACT_JS, value -> {
            keywords = BlogGenerator.filterKeywords(BlogGenerator.parseKeywordJson(value), 30);
            startGenerate();
        });
    }

    private void startGenerate() {
        if (finished) {
            return;
        }
        stage = Stage.GENERATING;
        notifyText("AI로 블로그 글을 생성하고 있어요");
        final String topic = AutoConfig.topic(this);
        final String base = AutoConfig.draftBase(this);
        final String openAiKey = AutoConfig.openAiKey(this);
        final String openAiModel = AutoConfig.openAiModel(this);
        final String geminiKey = AutoConfig.geminiKey(this);
        final String geminiModel = AutoConfig.geminiModel(this);
        final boolean imageSlots = useImageSlots;
        final boolean related = useRelated;
        final List<String> kw = new ArrayList<>(keywords);
        exec.execute(() -> {
            try {
                String prompt = BlogGenerator.buildBlogPrompt(topic, base, kw, imageSlots, related);
                String raw = BlogGenerator.generate(openAiKey, openAiModel, geminiKey, geminiModel, prompt);
                String cleaned = BlogGenerator.postProcess(raw, imageSlots);
                main.post(() -> onGenerated(cleaned));
            } catch (Exception e) {
                main.post(() -> {
                    notifyText("생성 실패: " + e.getMessage());
                    finishSelf();
                });
            }
        });
    }

    private void onGenerated(String content) {
        if (finished) {
            return;
        }
        generated = content == null ? "" : content;
        AutoConfig.setString(this, "last_result", generated);
        if (AutoConfig.TARGET_DRAFT.equals(publishTarget)) {
            notifyText("초안 생성 완료. 앱에서 확인하세요");
            finishSelf();
        } else if (AutoConfig.TARGET_APP.equals(publishTarget)) {
            launchNaverAppAndAutomate();
        } else {
            stage = Stage.PUBLISHING;
            notifyText("네이버 글쓰기 화면에서 자동 발행을 시도해요");
            web.loadUrl(NaverPublisher.writeUrl(account));
        }
    }

    private void fillThenPublish() {
        if (finished) {
            return;
        }
        String[] tb = NaverPublisher.splitTitleBody(generated);
        NaverPublisher.runFill(web, tb[0], tb[1], r1 -> {
            if (!autoPublish) {
                notifyText("본문 입력 완료. 수동 발행만 남았어요");
                main.postDelayed(this::finishSelf, 2000);
                return;
            }
            main.postDelayed(() -> NaverPublisher.runPublish(web, r2 -> {
                notifyText("자동 발행 시도 완료");
                main.postDelayed(this::finishSelf, 2500);
            }), 2500);
        });
    }

    private void launchNaverAppAndAutomate() {
        try {
            Intent launch = getPackageManager().getLaunchIntentForPackage(AutoConfig.NAVER_APP_PACKAGE);
            if (launch == null) {
                notifyText("네이버 블로그 앱이 설치되어 있지 않아요");
                finishSelf();
                return;
            }
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                    | Intent.FLAG_ACTIVITY_LAUNCH_ADJACENT
                    | Intent.FLAG_ACTIVITY_MULTIPLE_TASK);
            startActivity(launch);
            if (BlogAutoAccessibilityService.isRunning()) {
                main.postDelayed(() -> {
                    BlogAutoAccessibilityService svc = BlogAutoAccessibilityService.get();
                    if (svc != null) {
                        svc.automateNaverPost(generated, autoPublish);
                    }
                }, 2000);
                notifyText("네이버 앱에서 접근성 자동 입력을 진행해요");
                main.postDelayed(this::finishSelf, 15_000);
            } else {
                notifyText("접근성 서비스가 꺼져 있어 앱만 열었어요. 접근성을 켜 주세요");
                main.postDelayed(this::finishSelf, 3000);
            }
        } catch (Throwable t) {
            notifyText("네이버 앱 실행 실패: " + t.getMessage());
            finishSelf();
        }
    }

    private void finishSelf() {
        if (finished) {
            return;
        }
        finished = true;
        main.removeCallbacksAndMessages(null);
        if (web != null) {
            try {
                if (overlayAttached) {
                    WindowManager wm = (WindowManager) getSystemService(WINDOW_SERVICE);
                    if (wm != null) {
                        wm.removeView(web);
                    }
                }
            } catch (Throwable ignored) {
            }
            try {
                web.destroy();
            } catch (Throwable ignored) {
            }
            web = null;
        }
        releaseWake();
        exec.shutdownNow();
        stopForeground(true);
        stopSelf();
    }

    // ---------------------------------------------------------------
    //  포그라운드/알림/웨이크락
    // ---------------------------------------------------------------
    private void startForegroundSafely(String text) {
        ensureChannel();
        Notification n = buildNotification(text);
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(NOTI_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
            } else {
                startForeground(NOTI_ID, n);
            }
        } catch (Throwable t) {
            startForeground(NOTI_ID, n);
        }
    }

    private void ensureChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null && nm.getNotificationChannel(CHANNEL) == null) {
                NotificationChannel ch = new NotificationChannel(CHANNEL, "자동 글쓰기",
                        NotificationManager.IMPORTANCE_LOW);
                ch.setShowBadge(false);
                nm.createNotificationChannel(ch);
            }
        }
    }

    private Notification buildNotification(String text) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL)
                : new Notification.Builder(this);
        return b.setContentTitle("Picture Cleaner 자동 발행")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.ic_menu_edit)
                .setOngoing(true)
                .build();
    }

    private void notifyText(String text) {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (nm != null) {
            nm.notify(NOTI_ID, buildNotification(text));
        }
    }

    private void acquireWake() {
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm != null) {
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "picture:autopost");
                wakeLock.setReferenceCounted(false);
                wakeLock.acquire(HARD_TIMEOUT_MS + 30_000L);
            }
        } catch (Throwable ignored) {
        }
    }

    private void releaseWake() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
            }
        } catch (Throwable ignored) {
        }
        wakeLock = null;
    }

    @Override
    public void onDestroy() {
        finished = true;
        releaseWake();
        super.onDestroy();
    }
}
