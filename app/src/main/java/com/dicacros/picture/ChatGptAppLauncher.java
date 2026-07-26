package com.dicacros.picture;

import android.content.Context;
import android.content.Intent;

final class ChatGptAppLauncher {

    private ChatGptAppLauncher() {
    }

    static boolean launch(Context context) {
        try {
            Intent launch = context.getPackageManager()
                    .getLaunchIntentForPackage(AutoConfig.CHATGPT_APP_PACKAGE);
            if (launch == null) {
                return false;
            }
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                    | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
            context.startActivity(launch);
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }
}
