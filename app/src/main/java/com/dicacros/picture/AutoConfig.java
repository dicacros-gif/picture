package com.dicacros.picture;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;

/**
 * 모든 자동화 옵션의 단일 저장소. SharedPreferences 에 저장되므로 앱을 껏다 켜도,
 * 재부팅해도 사용자가 정의한 설정이 그대로 유지된다. 알람(1시간 주기) 예약/해제도 여기서 처리한다.
 */
final class AutoConfig {

    static final String PREFS = "picture_blog";
    private static final int ALARM_REQUEST = 7010;

    // 발행 방식.
    static final String TARGET_DRAFT = "draft";      // 초안만 생성/복사
    static final String TARGET_WEBVIEW = "webview";   // 앱 내 WebView 로 자동 발행
    static final String TARGET_APP = "app";           // 네이버 블로그 앱 + 접근성 자동 탭

    static final String NAVER_APP_PACKAGE = "com.nhn.android.blog";
    static final String CHATGPT_APP_PACKAGE = "com.openai.chatgpt";
    static final int INTERVAL_MINUTES = 60;

    private AutoConfig() {
    }

    static SharedPreferences prefs(Context ctx) {
        return ctx.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    // --- 문자열 옵션 ---
    static String account(Context c) {
        return prefs(c).getString("account", NaverAccounts.IDS[0]);
    }

    static String openAiKey(Context c) {
        return prefs(c).getString("openai_key", "");
    }

    static String geminiKey(Context c) {
        return prefs(c).getString("gemini_key", "");
    }

    static String openAiModel(Context c) {
        return orDefault(prefs(c).getString("openai_model", ""), "gpt-4.1");
    }

    static String geminiModel(Context c) {
        return orDefault(prefs(c).getString("gemini_model", ""), "gemini-2.5-flash");
    }

    static String draftBase(Context c) {
        return prefs(c).getString("draft_base", "");
    }

    static String publishTarget(Context c) {
        return prefs(c).getString("publish_target", TARGET_WEBVIEW);
    }

    static String lastResult(Context c) {
        return prefs(c).getString("last_result", "");
    }

    static String chatGptUrl(Context c) {
        return orDefault(prefs(c).getString("chatgpt_url", ""), "https://chatgpt.com/");
    }

    // --- 불리언 옵션 ---
    static boolean useImageSlots(Context c) {
        return prefs(c).getBoolean("opt_image", true);
    }

    static boolean autoEnabled(Context c) {
        return prefs(c).getBoolean("auto_enabled", false);
    }

    static boolean autoPublish(Context c) {
        return prefs(c).getBoolean("auto_publish", true);
    }

    static boolean useChatGptWeb(Context c) {
        return prefs(c).getBoolean("use_chatgpt_web", true);
    }

    static boolean useChatGptApp(Context c) {
        return prefs(c).getBoolean("use_chatgpt_app", false);
    }

    static boolean autoKeywordSelection(Context c) {
        return prefs(c).getBoolean("auto_keyword_selection", true);
    }

    // --- 정수 옵션 ---
    static int intervalMinutes(Context c) {
        return INTERVAL_MINUTES;
    }

    static void setString(Context c, String key, String value) {
        prefs(c).edit().putString(key, value).apply();
    }

    static void setBool(Context c, String key, boolean value) {
        prefs(c).edit().putBoolean(key, value).apply();
    }

    static void setInt(Context c, String key, int value) {
        prefs(c).edit().putInt(key, value).apply();
    }

    private static String orDefault(String v, String def) {
        return (v == null || v.trim().isEmpty()) ? def : v.trim();
    }

    // ---------------------------------------------------------------
    //  알람(주기 실행)
    // ---------------------------------------------------------------
    private static PendingIntent alarmIntent(Context ctx) {
        Intent intent = new Intent(ctx, AlarmReceiver.class);
        intent.setAction(AlarmReceiver.ACTION_FIRE);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) {
            flags |= PendingIntent.FLAG_IMMUTABLE;
        }
        return PendingIntent.getBroadcast(ctx, ALARM_REQUEST, intent, flags);
    }

    /** 지금부터 interval 분 뒤 1회 예약(발화 시마다 재예약하는 단발 반복 방식 — Doze 에서도 잘 깬다). */
    static void scheduleNext(Context ctx) {
        AlarmManager am = (AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
        if (am == null) {
            return;
        }
        long triggerAt = System.currentTimeMillis() + intervalMinutes(ctx) * 60_000L;
        PendingIntent pi = alarmIntent(ctx);
        try {
            if (Build.VERSION.SDK_INT >= 31 && !am.canScheduleExactAlarms()) {
                am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pi);
            } else if (Build.VERSION.SDK_INT >= 23) {
                am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pi);
            } else {
                am.setExact(AlarmManager.RTC_WAKEUP, triggerAt, pi);
            }
        } catch (SecurityException e) {
            am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pi);
        }
    }

    static void cancel(Context ctx) {
        AlarmManager am = (AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
        if (am != null) {
            am.cancel(alarmIntent(ctx));
        }
    }
}
