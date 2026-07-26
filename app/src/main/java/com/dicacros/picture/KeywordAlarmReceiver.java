package com.dicacros.picture;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
public class KeywordAlarmReceiver extends BroadcastReceiver {

    static final String ACTION_COLLECT =
            "com.dicacros.picture.COLLECT_REALTIME_KEYWORDS";

    @Override
    public void onReceive(Context context, Intent intent) {
        KeywordScheduler.collectNow(context);
        KeywordScheduler.scheduleNext(context);
    }
}
