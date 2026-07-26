package com.dicacros.picture;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

public class KeywordAlarmReceiver extends BroadcastReceiver {

    static final String ACTION_COLLECT =
            "com.dicacros.picture.COLLECT_REALTIME_KEYWORDS";

    @Override
    public void onReceive(Context context, Intent intent) {
        try {
            Intent service = new Intent(context, KeywordCollectorService.class);
            if (Build.VERSION.SDK_INT >= 26) {
                context.startForegroundService(service);
            } else {
                context.startService(service);
            }
        } catch (Throwable ignored) {
        }
        KeywordScheduler.scheduleNext(context);
    }
}
