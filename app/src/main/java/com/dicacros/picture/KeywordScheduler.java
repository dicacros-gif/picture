package com.dicacros.picture;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

final class KeywordScheduler {

    private static final int REQUEST_CODE = 7020;
    private static final String LEGACY_ACTION =
            "com.dicacros.picture.COLLECT_REALTIME_KEYWORDS";
    private static final String LEGACY_RECEIVER =
            "com.dicacros.picture.KeywordAlarmReceiver";

    private KeywordScheduler() {
    }

    static void collectNow(Context context) {
        try {
            Intent service = new Intent(context, KeywordCollectorService.class);
            if (Build.VERSION.SDK_INT >= 26) {
                context.startForegroundService(service);
            } else {
                context.startService(service);
            }
        } catch (Throwable ignored) {
        }
    }

    static void cancelScheduled(Context context) {
        AlarmManager alarmManager =
                (AlarmManager) context.getSystemService(Context.ALARM_SERVICE);
        Intent intent = new Intent();
        intent.setComponent(new ComponentName(context.getPackageName(), LEGACY_RECEIVER));
        intent.setAction(LEGACY_ACTION);
        int flags = PendingIntent.FLAG_NO_CREATE | PendingIntent.FLAG_IMMUTABLE;
        PendingIntent pendingIntent =
                PendingIntent.getBroadcast(context, REQUEST_CODE, intent, flags);
        if (pendingIntent != null) {
            if (alarmManager != null) {
                alarmManager.cancel(pendingIntent);
            }
            pendingIntent.cancel();
        }
    }
}
