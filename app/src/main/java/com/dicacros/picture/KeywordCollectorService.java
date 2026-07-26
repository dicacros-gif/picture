package com.dicacros.picture;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
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
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class KeywordCollectorService extends Service {

    private static final String ADSENSEFARM_URL = "https://adsensefarm.kr/realtime";
    private static final String SIGNAL_URL = "https://www.signal.bz/";
    private static final String CHANNEL = "keyword_collection";
    private static final int NOTIFICATION_ID = 4030;
    private static final long TIMEOUT_MS = 150_000L;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final List<KeywordDatabase.RankedKeyword> collected = new ArrayList<>();
    private WebView webView;
    private PowerManager.WakeLock wakeLock;
    private boolean overlayAttached;
    private boolean extractingAdsenseFarm;
    private boolean extractingSignal;
    private volatile boolean finished;

    @Override
    public IBinder onBind(android.content.Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(android.content.Intent intent, int flags, int startId) {
        if (webView != null && !finished) {
            return START_NOT_STICKY;
        }
        startForegroundSafely("실시간 검색어를 수집하고 있습니다.");
        acquireWakeLock();
        main.postDelayed(this::finishCollection, TIMEOUT_MS);
        try {
            webView = createWebView();
            webView.loadUrl(ADSENSEFARM_URL);
        } catch (Throwable throwable) {
            finishCollection();
        }
        return START_NOT_STICKY;
    }

    private WebView createWebView() {
        WebView view = new WebView(this);
        WebSettings settings = view.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        view.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView webView, String url) {
                if (!finished && url != null && url.contains("adsensefarm")) {
                    main.postDelayed(
                            KeywordCollectorService.this::extractAdsenseFarm, 1500);
                } else if (!finished && url != null && url.contains("signal.bz")) {
                    main.postDelayed(
                            KeywordCollectorService.this::extractSignal, 1500);
                }
            }
        });
        attachOverlay(view);
        return view;
    }

    private void extractAdsenseFarm() {
        if (finished || webView == null || extractingAdsenseFarm) {
            return;
        }
        extractingAdsenseFarm = true;
        webView.evaluateJavascript(RealtimeKeywordParser.EXTRACT_JS, value -> {
            collected.addAll(RealtimeKeywordParser.parse(value));
            if (!finished && webView != null) {
                updateNotification("애드센스팜 30개 수집 · 시그널을 확인합니다.");
                webView.loadUrl(SIGNAL_URL);
            }
        });
    }

    private void extractSignal() {
        if (finished || webView == null || extractingSignal) {
            return;
        }
        extractingSignal = true;
        webView.evaluateJavascript(SignalKeywordParser.EXTRACT_JS, value -> {
            collected.addAll(SignalKeywordParser.parse(value));
            storeCollection();
        });
    }

    private void storeCollection() {
        int rawCount = collected.size();
        List<KeywordDatabase.RankedKeyword> snapshot =
                RealtimeKeywordParser.filterContentCandidates(collected);
        int excludedCount = Math.max(0, rawCount - snapshot.size());
        executor.execute(() -> {
            try {
                KeywordAutomationEngine.Result recommendation = null;
                try (KeywordDatabase database = new KeywordDatabase(this)) {
                    database.pruneOlderThanDays(10);
                    database.upsertRankings(snapshot);
                    if (AutoConfig.autoKeywordSelection(this)) {
                        recommendation = KeywordAutomationEngine.enrichAndRecommend(
                                database, snapshot, 8, 12);
                    }
                }
                KeywordAutomationEngine.Result finalRecommendation = recommendation;
                main.post(() -> {
                    if (finished) {
                        return;
                    }
                    String message = rawCount + "개 수집 · 일회성 "
                            + excludedCount + "개 제외 · " + snapshot.size() + "개 저장";
                    if (finalRecommendation != null) {
                        message += " · 롱테일 "
                                + finalRecommendation.selected + "개 추천";
                    }
                    updateNotification(message);
                    main.postDelayed(this::finishCollection, 1200);
                });
            } catch (Throwable throwable) {
                main.post(() -> {
                    updateNotification("검색어 분석을 다음 회차에 다시 시도합니다.");
                    main.postDelayed(this::finishCollection, 1200);
                });
            }
        });
    }

    private void attachOverlay(WebView view) {
        if (Build.VERSION.SDK_INT >= 23 && !Settings.canDrawOverlays(this)) {
            return;
        }
        try {
            DisplayMetrics metrics = getResources().getDisplayMetrics();
            WindowManager.LayoutParams params = new WindowManager.LayoutParams(
                    Math.max(720, metrics.widthPixels),
                    Math.max(1280, metrics.heightPixels),
                    Build.VERSION.SDK_INT >= 26
                            ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                            : WindowManager.LayoutParams.TYPE_PHONE,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                            | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                            | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                    PixelFormat.TRANSLUCENT);
            params.gravity = Gravity.TOP | Gravity.START;
            view.setAlpha(0.02f);
            WindowManager manager =
                    (WindowManager) getSystemService(WINDOW_SERVICE);
            if (manager != null) {
                manager.addView(view, params);
                overlayAttached = true;
            }
        } catch (Throwable ignored) {
        }
    }

    private void startForegroundSafely(String text) {
        ensureChannel();
        Notification notification = buildNotification(text);
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(NOTIFICATION_ID, notification,
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
            } else {
                startForeground(NOTIFICATION_ID, notification);
            }
        } catch (Throwable throwable) {
            startForeground(NOTIFICATION_ID, notification);
        }
    }

    private void ensureChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager manager =
                    (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (manager != null && manager.getNotificationChannel(CHANNEL) == null) {
                NotificationChannel channel = new NotificationChannel(
                        CHANNEL, "실시간 검색어 수집", NotificationManager.IMPORTANCE_LOW);
                channel.setShowBadge(false);
                manager.createNotificationChannel(channel);
            }
        }
    }

    private Notification buildNotification(String text) {
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL)
                : new Notification.Builder(this);
        return builder.setContentTitle("Picture Cleaner 검색어 DB")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.ic_menu_search)
                .setOngoing(true)
                .build();
    }

    private void updateNotification(String text) {
        NotificationManager manager =
                (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager != null) {
            manager.notify(NOTIFICATION_ID, buildNotification(text));
        }
    }

    private void acquireWakeLock() {
        try {
            PowerManager manager =
                    (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (manager != null) {
                wakeLock = manager.newWakeLock(
                        PowerManager.PARTIAL_WAKE_LOCK, "picture:keywords");
                wakeLock.setReferenceCounted(false);
                wakeLock.acquire(TIMEOUT_MS + 10_000L);
            }
        } catch (Throwable ignored) {
        }
    }

    private void finishCollection() {
        if (finished) {
            return;
        }
        finished = true;
        main.removeCallbacksAndMessages(null);
        if (webView != null) {
            try {
                if (overlayAttached) {
                    WindowManager manager =
                            (WindowManager) getSystemService(WINDOW_SERVICE);
                    if (manager != null) {
                        manager.removeView(webView);
                    }
                }
            } catch (Throwable ignored) {
            }
            webView.destroy();
            webView = null;
        }
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
        executor.shutdownNow();
        stopForeground(true);
        stopSelf();
    }

    @Override
    public void onDestroy() {
        finishCollection();
        super.onDestroy();
    }
}
