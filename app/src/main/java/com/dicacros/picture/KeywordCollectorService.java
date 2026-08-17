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

public class KeywordCollectorService extends Service {

    static final String ACTION_COLLECTION_FINISHED =
            "com.dicacros.picture.KEYWORD_COLLECTION_FINISHED";
    static final String ACTION_CHALLENGE_REQUIRED =
            "com.dicacros.picture.KEYWORD_CHALLENGE_REQUIRED";
    static final String ACTION_CHALLENGE_CLEARED =
            "com.dicacros.picture.KEYWORD_CHALLENGE_CLEARED";
    static final String EXTRA_SNAPSHOT_ID = "snapshot_id";
    static final String EXTRA_MESSAGE = "message";
    static final String EXTRA_SUCCESS = "success";

    private static final String ACTION_RETRY_CHALLENGE =
            "com.dicacros.picture.RETRY_KEYWORD_CHALLENGE";
    private static final String ADSENSEFARM_URL = "https://adsensefarm.kr/realtime";
    private static final String SIGNAL_URL = "https://www.signal.bz/";
    private static final String CHANNEL = "keyword_collection";
    private static final int NOTIFICATION_ID = 4030;
    private static final long TIMEOUT_MS = 180_000L;
    private static final int MAX_EXTRACT_ATTEMPTS = 12;
    private static final int MAX_CHALLENGE_ATTEMPTS = 40;
    private static final long EXTRACT_RETRY_MS = 1000L;
    private static final long CHALLENGE_RETRY_MS = 3000L;

    private static volatile boolean collecting;
    private static volatile boolean challengeRequired;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final List<KeywordDatabase.RankedKeyword> adsenseRankings =
            new ArrayList<>();
    private final List<KeywordDatabase.RankedKeyword> signalRankings =
            new ArrayList<>();
    private final List<String> collectionErrors = new ArrayList<>();

    private RealtimeKeywordFetcher.Result directResult;
    private WebView webView;
    private PowerManager.WakeLock wakeLock;
    private boolean overlayAttached;
    private boolean extractingAdsenseFarm;
    private boolean extractingSignal;
    private boolean storageStarted;
    private int adsensePollGeneration;
    private volatile boolean finished;

    static boolean isCollecting() {
        return collecting;
    }

    static boolean isChallengeRequired() {
        return challengeRequired;
    }

    static void retryAfterChallenge(Context context) {
        Intent service = new Intent(context, KeywordCollectorService.class);
        service.setAction(ACTION_RETRY_CHALLENGE);
        try {
            context.startForegroundService(service);
        } catch (Throwable ignored) {
        }
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        boolean retryChallenge = intent != null
                && ACTION_RETRY_CHALLENGE.equals(intent.getAction());
        if (collecting) {
            if (retryChallenge) {
                retryLegacyCollection();
            }
            return START_NOT_STICKY;
        }

        collecting = true;
        startForegroundSafely("다음·Google·애드센스팜·시그널을 새로고침하고 있습니다.");
        acquireWakeLock();
        main.postDelayed(this::finishTimedOutCollection, TIMEOUT_MS);
        executor.execute(() -> {
            directResult = RealtimeKeywordFetcher.fetch();
            if (directResult != null) {
                collectionErrors.addAll(directResult.errors);
            }
            main.post(this::startLegacyCollection);
        });
        return START_NOT_STICKY;
    }

    private void startLegacyCollection() {
        if (finished) {
            return;
        }
        updateNotification("다음·Google 직접 수집 완료 · 애드센스팜을 확인합니다.");
        try {
            webView = createWebView();
            webView.loadUrl(ADSENSEFARM_URL);
        } catch (Throwable throwable) {
            collectionErrors.add("애드센스팜 WebView 시작 실패");
            storeCollection();
        }
    }

    private void retryLegacyCollection() {
        if (finished || webView == null) {
            return;
        }
        challengeRequired = false;
        broadcastChallenge(ACTION_CHALLENGE_CLEARED);
        extractingAdsenseFarm = false;
        extractingSignal = false;
        adsenseRankings.clear();
        signalRankings.clear();
        adsensePollGeneration++;
        updateNotification("로봇 확인 완료 · 애드센스팜을 다시 확인합니다.");
        webView.loadUrl(ADSENSEFARM_URL);
    }

    private WebView createWebView() {
        WebView view = new WebView(this);
        WebSettings settings = view.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(view, true);
        view.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView loadedView, String url) {
                if (!finished && url != null && url.contains("adsensefarm")) {
                    main.postDelayed(
                            KeywordCollectorService.this::extractAdsenseFarm, 1500L);
                } else if (!finished && url != null && url.contains("signal.bz")) {
                    main.postDelayed(
                            KeywordCollectorService.this::extractSignal, 1500L);
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
        int generation = ++adsensePollGeneration;
        pollAdsenseFarm(generation, 0);
    }

    private void pollAdsenseFarm(int generation, int attempt) {
        if (finished || webView == null || generation != adsensePollGeneration) {
            return;
        }
        webView.evaluateJavascript(RealtimeKeywordParser.EXTRACT_JS, value -> {
            if (generation != adsensePollGeneration) {
                return;
            }
            List<KeywordDatabase.RankedKeyword> rankings =
                    RealtimeKeywordParser.parse(value);
            if (rankings.size() < 30) {
                inspectChallenge(generation, rankings, attempt);
                return;
            }
            continueAfterAdsense(generation, rankings);
        });
    }

    private void inspectChallenge(
            int generation, List<KeywordDatabase.RankedKeyword> rankings, int attempt) {
        if (finished || webView == null || generation != adsensePollGeneration) {
            return;
        }
        webView.evaluateJavascript(RealtimeKeywordParser.CHALLENGE_JS, value -> {
            if (generation != adsensePollGeneration) {
                return;
            }
            boolean challenge = RealtimeKeywordParser.isChallenge(value);
            if (challenge) {
                if (!challengeRequired) {
                    challengeRequired = true;
                    broadcastChallenge(ACTION_CHALLENGE_REQUIRED);
                }
                updateNotification("애드센스팜 로봇 확인이 필요합니다.");
                if (attempt < MAX_CHALLENGE_ATTEMPTS) {
                    main.postDelayed(() -> {
                        if (finished || webView == null
                                || generation != adsensePollGeneration) {
                            return;
                        }
                        webView.loadUrl(ADSENSEFARM_URL);
                        main.postDelayed(
                                () -> pollAdsenseFarm(generation, attempt + 1),
                                1800L);
                    }, CHALLENGE_RETRY_MS);
                    return;
                }
                collectionErrors.add("애드센스팜 로봇 확인 시간 초과");
            } else if (attempt < MAX_EXTRACT_ATTEMPTS) {
                updateNotification("애드센스팜 로딩 " + rankings.size()
                        + "/30 · 다시 확인합니다.");
                main.postDelayed(
                        () -> pollAdsenseFarm(generation, attempt + 1),
                        EXTRACT_RETRY_MS);
                return;
            } else {
                collectionErrors.add("애드센스팜 " + rankings.size() + "/30 수집");
            }
            continueAfterAdsense(generation, rankings);
        });
    }

    private void continueAfterAdsense(
            int generation, List<KeywordDatabase.RankedKeyword> rankings) {
        if (finished || generation != adsensePollGeneration) {
            return;
        }
        if (challengeRequired) {
            challengeRequired = false;
            broadcastChallenge(ACTION_CHALLENGE_CLEARED);
        }
        adsenseRankings.clear();
        adsenseRankings.addAll(rankings);
        extractingSignal = false;
        updateNotification("애드센스팜 " + rankings.size()
                + "/30 수집 · 시그널을 확인합니다.");
        webView.loadUrl(SIGNAL_URL);
    }

    private void extractSignal() {
        if (finished || webView == null || extractingSignal) {
            return;
        }
        extractingSignal = true;
        pollSignal(0);
    }

    private void pollSignal(int attempt) {
        if (finished || webView == null) {
            return;
        }
        webView.evaluateJavascript(SignalKeywordParser.EXTRACT_JS, value -> {
            List<KeywordDatabase.RankedKeyword> rankings =
                    SignalKeywordParser.parse(value);
            if (rankings.size() < 10 && attempt < MAX_EXTRACT_ATTEMPTS) {
                updateNotification("시그널 로딩 " + rankings.size()
                        + "/10 · 다시 확인합니다.");
                main.postDelayed(
                        () -> pollSignal(attempt + 1), EXTRACT_RETRY_MS);
                return;
            }
            if (rankings.size() < 10) {
                collectionErrors.add("시그널 " + rankings.size() + "/10 수집");
            }
            signalRankings.clear();
            signalRankings.addAll(rankings);
            storeCollection();
        });
    }

    private void storeCollection() {
        if (finished || storageStarted) {
            return;
        }
        storageStarted = true;
        adsensePollGeneration++;
        if (webView != null) {
            webView.stopLoading();
        }
        List<KeywordDatabase.RankedKeyword> collected = new ArrayList<>();
        int daumDirectCount = 0;
        int googleDirectCount = 0;
        if (directResult != null) {
            collected.addAll(directResult.rankings);
            daumDirectCount = directResult.daumCount;
            googleDirectCount = directResult.googleCount;
        }
        collected.addAll(adsenseRankings);
        collected.addAll(signalRankings);
        int adsenseCount = adsenseRankings.size();
        int signalCount = signalRankings.size();
        List<String> errors = new ArrayList<>(collectionErrors);
        int rawCount = collected.size();
        List<KeywordDatabase.RankedKeyword> filtered =
                RealtimeKeywordParser.filterContentCandidates(collected);
        int excludedCount = Math.max(0, rawCount - filtered.size());
        if (filtered.isEmpty()) {
            finishWithBroadcast(
                    false, -1L, "네 출처에서 저장할 검색어를 가져오지 못했습니다.");
            return;
        }

        int finalDaumDirectCount = daumDirectCount;
        int finalGoogleDirectCount = googleDirectCount;
        executor.execute(() -> {
            long snapshotId;
            try (KeywordDatabase database = new KeywordDatabase(this)) {
                database.pruneOlderThanDays(7);
                snapshotId = database.saveSnapshot(filtered, rawCount);
                database.retainSingleSelection();
                if (AutoConfig.autoKeywordSelection(this)) {
                    KeywordAutomationEngine.enrichAndRecommend(
                            database, filtered, 8, 1);
                }
            } catch (Throwable throwable) {
                main.post(() -> finishWithBroadcast(
                        false, -1L, "검색어 DB 저장에 실패했습니다."));
                return;
            }

            String message = "직접 다음 " + finalDaumDirectCount
                    + " · 직접 구글 " + finalGoogleDirectCount
                    + " · 애드센스팜 " + adsenseCount
                    + " · 시그널 " + signalCount
                    + " 수집 · 중복·일회성 " + excludedCount
                    + "개 제외 · " + filtered.size() + "개 저장";
            if (!errors.isEmpty()) {
                message += " · 일부: "
                        + BlogGenerator.join(errors, " / ");
            }
            long finalSnapshotId = snapshotId;
            String finalMessage = message;
            main.post(() -> finishWithBroadcast(
                    finalSnapshotId > 0L, finalSnapshotId, finalMessage));
        });
    }

    private void finishTimedOutCollection() {
        if (finished || storageStarted) {
            return;
        }
        collectionErrors.add("애드센스팜·시그널 수집 시간 초과");
        storeCollection();
    }

    private void finishWithBroadcast(boolean success, long snapshotId, String message) {
        if (finished) {
            return;
        }
        finished = true;
        challengeRequired = false;
        broadcastChallenge(ACTION_CHALLENGE_CLEARED);
        updateNotification(message);
        Intent completed = new Intent(ACTION_COLLECTION_FINISHED);
        completed.setPackage(getPackageName());
        completed.putExtra(EXTRA_SUCCESS, success);
        completed.putExtra(EXTRA_SNAPSHOT_ID, snapshotId);
        completed.putExtra(EXTRA_MESSAGE, message);
        sendBroadcast(completed);
        main.postDelayed(this::stopCollection, 900L);
    }

    private void broadcastChallenge(String action) {
        Intent broadcast = new Intent(action);
        broadcast.setPackage(getPackageName());
        sendBroadcast(broadcast);
    }

    private void attachOverlay(WebView view) {
        if (!Settings.canDrawOverlays(this)) {
            return;
        }
        try {
            DisplayMetrics metrics = getResources().getDisplayMetrics();
            WindowManager.LayoutParams params = new WindowManager.LayoutParams(
                    Math.max(720, metrics.widthPixels),
                    Math.max(1280, metrics.heightPixels),
                    WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
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
        NotificationManager manager =
                (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager != null && manager.getNotificationChannel(CHANNEL) == null) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL, "실시간 검색어 새로고침",
                    NotificationManager.IMPORTANCE_LOW);
            channel.setShowBadge(false);
            manager.createNotificationChannel(channel);
        }
    }

    private Notification buildNotification(String text) {
        return new Notification.Builder(this, CHANNEL)
                .setContentTitle("Picture Cleaner 검색어")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.ic_menu_search)
                .setOngoing(!finished)
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

    private void stopCollection() {
        main.removeCallbacksAndMessages(null);
        collecting = false;
        destroyWebView();
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
        executor.shutdownNow();
        stopForeground(true);
        stopSelf();
    }

    private void destroyWebView() {
        if (webView == null) {
            return;
        }
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
        webView.stopLoading();
        webView.destroy();
        webView = null;
        overlayAttached = false;
    }

    @Override
    public void onDestroy() {
        collecting = false;
        challengeRequired = false;
        main.removeCallbacksAndMessages(null);
        destroyWebView();
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
        executor.shutdownNow();
        super.onDestroy();
    }
}
