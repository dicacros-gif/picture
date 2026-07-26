package com.dicacros.picture;

import android.content.ClipData;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;

import java.util.ArrayList;
import java.util.List;

final class NaverAppLauncher {

    private NaverAppLauncher() {
    }

    static Result launch(Context context, List<Uri> imageUris) {
        if (imageUris != null && !imageUris.isEmpty()) {
            try {
                ArrayList<Uri> streams = new ArrayList<>(imageUris);
                Intent share = new Intent(Intent.ACTION_SEND_MULTIPLE);
                share.setPackage(AutoConfig.NAVER_APP_PACKAGE);
                share.setType("image/*");
                share.putParcelableArrayListExtra(Intent.EXTRA_STREAM, streams);
                share.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                        | Intent.FLAG_ACTIVITY_NEW_TASK
                        | Intent.FLAG_ACTIVITY_LAUNCH_ADJACENT
                        | Intent.FLAG_ACTIVITY_MULTIPLE_TASK);
                ClipData clipData = ClipData.newUri(
                        context.getContentResolver(), "PictureCleaner", streams.get(0));
                for (int index = 1; index < streams.size(); index++) {
                    clipData.addItem(new ClipData.Item(streams.get(index)));
                }
                share.setClipData(clipData);
                context.startActivity(share);
                return new Result(true, true);
            } catch (Throwable ignored) {
            }
        }
        try {
            Intent launch = context.getPackageManager()
                    .getLaunchIntentForPackage(AutoConfig.NAVER_APP_PACKAGE);
            if (launch == null) {
                return new Result(false, false);
            }
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                    | Intent.FLAG_ACTIVITY_LAUNCH_ADJACENT
                    | Intent.FLAG_ACTIVITY_MULTIPLE_TASK);
            context.startActivity(launch);
            return new Result(true, false);
        } catch (Throwable ignored) {
            return new Result(false, false);
        }
    }

    static final class Result {
        final boolean launched;
        final boolean sharedImages;

        Result(boolean launched, boolean sharedImages) {
            this.launched = launched;
            this.sharedImages = sharedImages;
        }
    }
}
