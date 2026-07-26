package com.dicacros.picture;

import android.content.ContentResolver;
import android.content.ContentUris;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.provider.MediaStore;

import java.util.ArrayList;
import java.util.Calendar;
import java.util.List;

final class ProcessedImageStore {

    private ProcessedImageStore() {
    }

    static List<Uri> todayImages(Context context, int maxCount) {
        List<Uri> images = new ArrayList<>();
        ContentResolver resolver = context.getContentResolver();
        Uri collection = MediaStore.Images.Media.EXTERNAL_CONTENT_URI;
        String[] projection = {MediaStore.Images.Media._ID};

        Calendar start = Calendar.getInstance();
        start.set(Calendar.HOUR_OF_DAY, 0);
        start.set(Calendar.MINUTE, 0);
        start.set(Calendar.SECOND, 0);
        start.set(Calendar.MILLISECOND, 0);

        String selection;
        String[] args;
        if (Build.VERSION.SDK_INT >= 29) {
            selection = MediaStore.Images.Media.DATE_ADDED + ">=? AND "
                    + MediaStore.Images.Media.RELATIVE_PATH + " LIKE ?";
            args = new String[]{
                    String.valueOf(start.getTimeInMillis() / 1000L),
                    "%PictureCleaner%"};
        } else {
            selection = MediaStore.Images.Media.DATE_ADDED + ">=? AND ("
                    + MediaStore.Images.Media.DISPLAY_NAME + " LIKE ? OR "
                    + MediaStore.Images.Media.DISPLAY_NAME + " LIKE ?)";
            args = new String[]{
                    String.valueOf(start.getTimeInMillis() / 1000L),
                    "cropped_%", "remastered_%"};
        }
        String sort = MediaStore.Images.Media.DATE_ADDED + " ASC";
        try (Cursor cursor = resolver.query(
                collection, projection, selection, args, sort)) {
            if (cursor == null) {
                return images;
            }
            int idColumn = cursor.getColumnIndexOrThrow(MediaStore.Images.Media._ID);
            int limit = Math.max(1, Math.min(30, maxCount));
            while (cursor.moveToNext() && images.size() < limit) {
                images.add(ContentUris.withAppendedId(
                        collection, cursor.getLong(idColumn)));
            }
        } catch (Throwable ignored) {
        }
        return images;
    }
}
