package com.dicacros.picture;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * 재부팅 후에도 사용자가 켜 둔 자동화가 이어지도록 알람을 다시 예약한다.
 */
public class BootReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (action == null) {
            return;
        }
        if (Intent.ACTION_BOOT_COMPLETED.equals(action)
                || "android.intent.action.QUICKBOOT_POWERON".equals(action)) {
            if (AutoConfig.autoEnabled(context)) {
                AutoConfig.scheduleNext(context);
            }
        }
    }
}
