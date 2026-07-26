package com.dicacros.picture;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

final class KeywordScheduler {

    private static final int REQUEST_CODE = 7020;
    private static final long INTERVAL_MS = 60L * 60L * 1000L;

    private KeywordScheduler() {
    }

    static void ensureScheduled(Context context) {
        schedule(context, System.currentTimeMillis() + INTERVAL_MS);
    }

    static void scheduleNext(Context context) {
        schedule(context, System.currentTimeMillis() + INTERVAL_MS);
    }

    private static void schedule(Context context, long triggerAt) {
        AlarmManager alarmManager =
                (AlarmManager) context.getSystemService(Context.ALARM_SERVICE);
        if (alarmManager == null) {
            return;
        }
        PendingIntent pendingIntent = pendingIntent(context);
        try {
            if (Build.VERSION.SDK_INT >= 31 && !alarmManager.canScheduleExactAlarms()) {
                alarmManager.setAndAllowWhileIdle(
                        AlarmManager.RTC_WAKEUP, triggerAt, pendingIntent);
            } else {
                alarmManager.setExactAndAllowWhileIdle(
                        AlarmManager.RTC_WAKEUP, triggerAt, pendingIntent);
            }
        } catch (SecurityException exception) {
            alarmManager.setAndAllowWhileIdle(
                    AlarmManager.RTC_WAKEUP, triggerAt, pendingIntent);
        }
    }

    private static PendingIntent pendingIntent(Context context) {
        Intent intent = new Intent(context, KeywordAlarmReceiver.class);
        intent.setAction(KeywordAlarmReceiver.ACTION_COLLECT);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
        return PendingIntent.getBroadcast(context, REQUEST_CODE, intent, flags);
    }
}
