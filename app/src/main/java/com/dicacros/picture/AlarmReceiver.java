package com.dicacros.picture;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

/**
 * 1시간(설정값)마다 알람이 발화하면 백그라운드 자동 발행 서비스를 깨우고 다음 알람을 재예약한다.
 * 단발 알람을 매번 다시 거는 방식이라 Doze 상태에서도 비교적 정확히 깬다.
 */
public class AlarmReceiver extends BroadcastReceiver {

    static final String ACTION_FIRE = "com.dicacros.picture.AUTO_FIRE";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (!AutoConfig.autoEnabled(context)) {
            return;
        }
        try {
            Intent service = new Intent(context, BlogAutoService.class);
            service.putExtra(BlogAutoService.EXTRA_REASON, "alarm");
            if (Build.VERSION.SDK_INT >= 26) {
                context.startForegroundService(service);
            } else {
                context.startService(service);
            }
        } catch (Throwable t) {
            // 백그라운드 FGS 시작 제한 등으로 실패해도 앱이 죽지 않게 하고 다음 회차는 계속 예약한다.
        }
        // 다음 회차 예약(단발 반복) — 서비스 시작 성공 여부와 무관하게 항상 재예약.
        AutoConfig.scheduleNext(context);
    }
}
