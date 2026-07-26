package com.dicacros.picture;

import android.Manifest;
import android.app.Activity;
import android.app.PendingIntent;
import android.content.ContentResolver;
import android.content.ContentUris;
import android.content.ContentValues;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.Rect;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.provider.Settings;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Picture Cleaner — 오늘 캡처한 스크린샷을 한 버튼으로 크롭 + 해상도 개선한다.
 *
 * 버튼 ①: 글자/검정/단색 여백을 제외한 이미지 영역만 자동 크롭한 뒤 곧바로 해상도를 키우고 선명하게
 * 개선해 새 JPEG(캡처 메타데이터 제거)로 저장하고, 원본은 자동으로 삭제한다.
 * '모든 파일 접근' 권한이 있으면 삭제 확인창 없이 바로 지운다.
 */
public class MainActivity extends Activity {
    private static final int REQUEST_READ_IMAGES = 1001;
    private static final int REQUEST_DELETE_IMAGES = 1002;
    private static final String OUTPUT_FOLDER = "Pictures/PictureCleaner";
    private static final String PREFS = "picture_main";

    private static final int ANALYSIS_LONG_SIDE = 900;
    private static final int STRIP_ROWS = 256;
    private static final int MAX_DECODE_PIXELS = 32 * 1024 * 1024;
    private static final int TARGET_LONG = 2048;
    private static final int MAX_LONG_SIDE = 4096;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private TextView statusText;
    private ProgressBar progressBar;
    private Button cropButton;
    private Button keywordButton;
    private Button blogWriterButton;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(createContentView());
        KeywordScheduler.ensureScheduled(this);
        maybeRequestAllFilesAccess();
        updateStatus("오늘 캡처한 스크린샷을 크롭하고 해상도를 개선할 준비가 됐습니다.");
    }

    @Override
    protected void onDestroy() {
        executor.shutdownNow();
        super.onDestroy();
    }

    private View createContentView() {
        ScrollView scrollView = new ScrollView(this);
        scrollView.setFillViewport(true);
        scrollView.setBackgroundColor(UiKit.BACKGROUND);
        LinearLayout root = UiKit.screen(this);
        scrollView.addView(root);

        LinearLayout hero = UiKit.tintedCard(
                this, UiKit.INFO_SOFT, Color.rgb(191, 219, 254));
        hero.setPadding(dp(18), dp(20), dp(18), dp(20));
        hero.addView(UiKit.eyebrow(this, "PICTURE CLEANER"));
        hero.addView(UiKit.pageTitle(this, "오늘의 캡처를\n콘텐츠로 바꾸세요"));
        hero.addView(UiKit.body(this,
                "이미지 정리부터 검색어 분석, 블로그 작성까지 한 흐름으로 진행합니다."));
        root.addView(hero);

        cropButton = createPrimaryButton("이미지 정리 시작", UiKit.PRIMARY);
        cropButton.setOnClickListener(v -> startProcessing());
        root.addView(actionCard(
                "STEP 1", "이미지 자동 정리",
                "오늘 캡처한 사진에서 불필요한 글자와 여백을 제거하고 해상도를 개선합니다.",
                cropButton, UiKit.PRIMARY));

        keywordButton = createPrimaryButton("실시간 연관 검색어", UiKit.TEAL);
        keywordButton.setOnClickListener(
                v -> startActivity(new Intent(this, KeywordActivity.class)));
        root.addView(actionCard(
                "STEP 2", "실시간 연관 검색어",
                "다음·구글·네이버 실시간 순위 30개를 모아 오래 검색될 주제와 연관 질문을 추천합니다.",
                keywordButton, UiKit.TEAL));

        blogWriterButton = createPrimaryButton("블로그 자동화 열기", UiKit.NAVY);
        blogWriterButton.setOnClickListener(v -> startActivity(new Intent(this, BlogWriterActivity.class)));
        root.addView(actionCard(
                "STEP 3", "네이버 블로그 자동화",
                "선택한 롱테일 주제와 정리된 이미지를 이용해 생성부터 발행까지 연결합니다.",
                blogWriterButton, UiKit.NAVY));

        LinearLayout statusCard = UiKit.card(this);
        statusCard.addView(UiKit.sectionTitle(this, "작업 상태"));
        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        progressBar.setProgress(0);
        UiKit.tintProgress(progressBar, UiKit.PRIMARY);
        LinearLayout.LayoutParams progressParams = new LinearLayout.LayoutParams(-1, dp(8));
        progressParams.setMargins(0, dp(4), 0, dp(4));
        statusCard.addView(progressBar, progressParams);

        statusText = UiKit.status(this);
        statusCard.addView(statusText);
        root.addView(statusCard);

        return scrollView;
    }

    private LinearLayout actionCard(String step, String title, String description,
                                    Button button, int accent) {
        LinearLayout card = UiKit.card(this);
        card.addView(UiKit.badge(this, step, accent));
        card.addView(UiKit.sectionTitle(this, title));
        TextView descriptionView = UiKit.body(this, description);
        LinearLayout.LayoutParams descriptionParams =
                new LinearLayout.LayoutParams(-1, -2);
        descriptionParams.setMargins(0, 0, 0, dp(14));
        card.addView(descriptionView, descriptionParams);
        card.addView(button);
        return card;
    }

    private Button createPrimaryButton(String text, int color) {
        Button button = UiKit.primaryButton(this, text, color);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(50));
        button.setLayoutParams(params);
        return button;
    }

    // ---------------------------------------------------------------
    //  권한
    // ---------------------------------------------------------------
    private void startProcessing() {
        if (!hasImagePermission()) {
            requestImagePermission();
            return;
        }
        setControlsEnabled(false);
        progressBar.setProgress(0);
        updateStatus("오늘 스크린샷을 찾는 중입니다...");
        executor.execute(this::processTodayScreenshots);
    }

    private boolean hasImagePermission() {
        if (Build.VERSION.SDK_INT >= 33) {
            return checkSelfPermission(Manifest.permission.READ_MEDIA_IMAGES) == PackageManager.PERMISSION_GRANTED;
        }
        return checkSelfPermission(Manifest.permission.READ_EXTERNAL_STORAGE) == PackageManager.PERMISSION_GRANTED;
    }

    private void requestImagePermission() {
        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(new String[]{Manifest.permission.READ_MEDIA_IMAGES}, REQUEST_READ_IMAGES);
        } else {
            requestPermissions(new String[]{Manifest.permission.READ_EXTERNAL_STORAGE}, REQUEST_READ_IMAGES);
        }
    }

    /** 원본을 확인창 없이 자동 삭제하려면 '모든 파일 접근'이 필요하다. 최초 1회만 안내 화면을 연다. */
    private void maybeRequestAllFilesAccess() {
        if (Build.VERSION.SDK_INT < 30 || Environment.isExternalStorageManager()) {
            return;
        }
        boolean asked = getSharedPreferences(PREFS, MODE_PRIVATE).getBoolean("asked_all_files", false);
        if (asked) {
            return;
        }
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putBoolean("asked_all_files", true).apply();
        try {
            Intent intent = new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                    Uri.parse("package:" + getPackageName()));
            startActivity(intent);
        } catch (Exception ignored) {
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQUEST_READ_IMAGES && grantResults.length > 0
                && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            startProcessing();
        } else if (requestCode == REQUEST_READ_IMAGES) {
            updateStatus("사진 읽기 권한이 필요합니다.");
        }
    }

    // ---------------------------------------------------------------
    //  처리
    // ---------------------------------------------------------------
    private void processTodayScreenshots() {
        ContentResolver resolver = getContentResolver();
        List<ImageItem> items = queryTodayScreenshots(resolver);
        if (items.isEmpty()) {
            finishOnUi("오늘 캡처한 스크린샷을 찾지 못했습니다.", 0);
            return;
        }

        List<ImageItem> processedOriginals = new ArrayList<>();
        int savedCount = 0;
        int failedCount = 0;
        for (int index = 0; index < items.size(); index++) {
            ImageItem item = items.get(index);
            updateProgress(index, items.size(), item.displayName + " 처리 중...");
            Bitmap source = null;
            Bitmap cropped = null;
            Bitmap upscaled = null;
            Bitmap out = null;
            try {
                source = decodeBitmap(resolver, item.uri);
                if (source == null) {
                    failedCount++;
                    continue;
                }
                cropped = cropContent(source);
                upscaled = upscaleIfSmall(cropped, TARGET_LONG, MAX_LONG_SIDE);
                out = sharpenStripwise(upscaled, 0.5f);
                saveBitmap(resolver, out, "cropped_" + timestampName(index) + ".jpg");
                savedCount++;
                processedOriginals.add(item);
            } catch (Throwable throwable) {
                failedCount++;
            } finally {
                if (out != null) {
                    out.recycle();
                }
                if (upscaled != null && upscaled != cropped && upscaled != source) {
                    upscaled.recycle();
                }
                if (cropped != null && cropped != source) {
                    cropped.recycle();
                }
                if (source != null) {
                    source.recycle();
                }
            }
        }

        int finalSaved = savedCount;
        int finalFailed = failedCount;
        runOnUiThread(() -> {
            progressBar.setProgress(100);
            setControlsEnabled(true);
            String message = finalSaved + "개 크롭·개선 저장 완료";
            if (finalFailed > 0) {
                message += ", " + finalFailed + "개 실패";
            }
            updateStatus(message + ".");
            deleteOriginals(processedOriginals);
        });
    }

    private List<ImageItem> queryTodayScreenshots(ContentResolver resolver) {
        List<ImageItem> items = new ArrayList<>();
        Uri collection = MediaStore.Images.Media.EXTERNAL_CONTENT_URI;
        String[] projection = {
                MediaStore.Images.Media._ID,
                MediaStore.Images.Media.DISPLAY_NAME,
                MediaStore.Images.Media.DATA,
                MediaStore.Images.Media.DATE_ADDED,
                MediaStore.Images.Media.RELATIVE_PATH
        };

        Calendar start = Calendar.getInstance();
        start.set(Calendar.HOUR_OF_DAY, 0);
        start.set(Calendar.MINUTE, 0);
        start.set(Calendar.SECOND, 0);
        start.set(Calendar.MILLISECOND, 0);
        long startSeconds = start.getTimeInMillis() / 1000L;

        String selection = MediaStore.Images.Media.DATE_ADDED + ">=? AND (" +
                MediaStore.Images.Media.DISPLAY_NAME + " LIKE ? OR " +
                MediaStore.Images.Media.RELATIVE_PATH + " LIKE ?) AND " +
                MediaStore.Images.Media.RELATIVE_PATH + " NOT LIKE ?";
        String[] args = {String.valueOf(startSeconds), "Screenshot%", "%Screenshots%", "%PictureCleaner%"};
        String sort = MediaStore.Images.Media.DATE_ADDED + " ASC";

        try (android.database.Cursor cursor = resolver.query(collection, projection, selection, args, sort)) {
            if (cursor == null) {
                return items;
            }
            int idColumn = cursor.getColumnIndexOrThrow(MediaStore.Images.Media._ID);
            int nameColumn = cursor.getColumnIndexOrThrow(MediaStore.Images.Media.DISPLAY_NAME);
            int dataColumn = cursor.getColumnIndex(MediaStore.Images.Media.DATA);
            while (cursor.moveToNext()) {
                long id = cursor.getLong(idColumn);
                String displayName = cursor.getString(nameColumn);
                String data = dataColumn >= 0 ? cursor.getString(dataColumn) : null;
                Uri uri = ContentUris.withAppendedId(collection, id);
                items.add(new ImageItem(uri, displayName, data));
            }
        }
        return items;
    }

    private Bitmap decodeBitmap(ContentResolver resolver, Uri uri) throws IOException {
        BitmapFactory.Options bounds = new BitmapFactory.Options();
        bounds.inJustDecodeBounds = true;
        try (InputStream stream = resolver.openInputStream(uri)) {
            BitmapFactory.decodeStream(stream, null, bounds);
        }
        int sample = 1;
        long pixels = (long) bounds.outWidth * bounds.outHeight;
        while (pixels > 0 && pixels / (sample * sample) > MAX_DECODE_PIXELS) {
            sample *= 2;
        }
        BitmapFactory.Options options = new BitmapFactory.Options();
        options.inSampleSize = sample;
        options.inPreferredConfig = Bitmap.Config.ARGB_8888;
        try (InputStream stream = resolver.openInputStream(uri)) {
            return BitmapFactory.decodeStream(stream, null, options);
        }
    }

    // ---------------------------------------------------------------
    //  크롭
    // ---------------------------------------------------------------
    private Bitmap cropContent(Bitmap source) {
        Rect bounds = detectContentBounds(source);
        if (bounds.width() <= 0 || bounds.height() <= 0
                || (bounds.width() == source.getWidth() && bounds.height() == source.getHeight())) {
            return source;
        }
        return Bitmap.createBitmap(source, bounds.left, bounds.top, bounds.width(), bounds.height());
    }

    private Rect detectContentBounds(Bitmap fullBitmap) {
        int fullW = fullBitmap.getWidth();
        int fullH = fullBitmap.getHeight();
        float scale = Math.min(1f, ANALYSIS_LONG_SIDE / (float) Math.max(fullW, fullH));
        int aw = Math.max(1, Math.round(fullW * scale));
        int ah = Math.max(1, Math.round(fullH * scale));
        Bitmap small = (aw == fullW && ah == fullH)
                ? fullBitmap
                : Bitmap.createScaledBitmap(fullBitmap, aw, ah, true);

        int[] px = new int[aw * ah];
        small.getPixels(px, 0, aw, 0, 0, aw, ah);
        if (small != fullBitmap) {
            small.recycle();
        }

        boolean[] content = new boolean[aw * ah];
        for (int y = 0; y < ah; y++) {
            for (int x = 0; x < aw; x++) {
                int idx = y * aw + x;
                int c = px[idx];
                int r = (c >> 16) & 0xFF, g = (c >> 8) & 0xFF, b = c & 0xFF;
                int max = Math.max(r, Math.max(g, b));
                int min = Math.min(r, Math.min(g, b));
                int sat = max - min;
                int lum = (r * 299 + g * 587 + b * 114) / 1000;
                int edge = 0;
                if (x + 1 < aw) {
                    edge += Math.abs(lum - lumOf(px[idx + 1]));
                }
                if (y + 1 < ah) {
                    edge += Math.abs(lum - lumOf(px[idx + aw]));
                }
                boolean flat = (lum <= 30 || lum >= 248) && sat < 14 && edge < 16;
                content[idx] = !flat && (sat > 18 || edge > 22);
            }
        }

        int[] rowScore = new int[ah];
        for (int y = 0; y < ah; y++) {
            int score = 0;
            int base = y * aw;
            for (int x = 0; x < aw; x++) {
                if (content[base + x]) {
                    score++;
                }
            }
            rowScore[y] = score;
        }
        boolean[] rowActive = smoothActive(rowScore, Math.max(1, aw / 12), 1);
        int[] vBand = longestRun(rowActive);
        int top = vBand[0];
        int bottom = vBand[1];
        if (bottom - top < ah / 8) {
            return new Rect(0, 0, fullW, fullH);
        }

        int[] colScore = new int[aw];
        for (int x = 0; x < aw; x++) {
            int score = 0;
            for (int y = top; y < bottom; y++) {
                if (content[y * aw + x]) {
                    score++;
                }
            }
            colScore[x] = score;
        }
        boolean[] colActive = smoothActive(colScore, Math.max(1, (bottom - top) / 12), 1);
        int[] hBand = longestRun(colActive);
        int left = hBand[0];
        int right = hBand[1];
        if (right - left < aw / 8) {
            left = 0;
            right = aw;
        }

        float inv = 1f / scale;
        int pad = Math.max(2, Math.min(fullW, fullH) / 300);
        int fLeft = clampInt(Math.round(left * inv) - pad, 0, fullW - 1);
        int fTop = clampInt(Math.round(top * inv) - pad, 0, fullH - 1);
        int fRight = clampInt(Math.round(right * inv) + pad, fLeft + 1, fullW);
        int fBottom = clampInt(Math.round(bottom * inv) + pad, fTop + 1, fullH);
        return new Rect(fLeft, fTop, fRight, fBottom);
    }

    private int lumOf(int c) {
        return (((c >> 16) & 0xFF) * 299 + ((c >> 8) & 0xFF) * 587 + (c & 0xFF) * 114) / 1000;
    }

    private boolean[] smoothActive(int[] score, int threshold, int radius) {
        int n = score.length;
        boolean[] active = new boolean[n];
        int max = 1;
        for (int s : score) {
            max = Math.max(max, s);
        }
        int thresh = Math.max(threshold, max / 6);
        boolean[] raw = new boolean[n];
        for (int i = 0; i < n; i++) {
            raw[i] = score[i] >= thresh;
        }
        for (int i = 0; i < n; i++) {
            boolean on = false;
            for (int d = -radius; d <= radius && !on; d++) {
                int j = i + d;
                if (j >= 0 && j < n && raw[j]) {
                    on = true;
                }
            }
            active[i] = on;
        }
        return active;
    }

    private int[] longestRun(boolean[] active) {
        int n = active.length;
        int bestStart = 0, bestLen = 0;
        int start = -1;
        for (int i = 0; i <= n; i++) {
            boolean on = i < n && active[i];
            if (on && start < 0) {
                start = i;
            } else if (!on && start >= 0) {
                int len = i - start;
                if (len > bestLen) {
                    bestLen = len;
                    bestStart = start;
                }
                start = -1;
            }
        }
        if (bestLen == 0) {
            return new int[]{0, n};
        }
        return new int[]{bestStart, bestStart + bestLen};
    }

    // ---------------------------------------------------------------
    //  해상도 개선 (업스케일 + 언샤프)
    // ---------------------------------------------------------------
    private Bitmap upscaleIfSmall(Bitmap source, int targetLongSide, int maxLongSide) {
        int longSide = Math.max(source.getWidth(), source.getHeight());
        if (longSide >= targetLongSide) {
            return source;
        }
        float scale = Math.min(maxLongSide / (float) longSide, targetLongSide / (float) longSide);
        int newW = Math.round(source.getWidth() * scale);
        int newH = Math.round(source.getHeight() * scale);
        return Bitmap.createScaledBitmap(source, Math.max(1, newW), Math.max(1, newH), true);
    }

    private Bitmap sharpenStripwise(Bitmap source, float amount) {
        int w = source.getWidth();
        int h = source.getHeight();
        if (w < 3 || h < 3) {
            return source.copy(Bitmap.Config.ARGB_8888, false);
        }
        Bitmap out = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
        int y = 0;
        while (y < h) {
            int rows = Math.min(STRIP_ROWS, h - y);
            int readStart = Math.max(0, y - 1);
            int readEnd = Math.min(h, y + rows + 1);
            int readRows = readEnd - readStart;
            int[] in = new int[w * readRows];
            source.getPixels(in, 0, w, 0, readStart, w, readRows);
            int[] outBuf = new int[w * rows];
            for (int ry = 0; ry < rows; ry++) {
                int gy = y + ry;
                int inY = gy - readStart;
                for (int x = 0; x < w; x++) {
                    int center = in[inY * w + x];
                    if (x == 0 || x == w - 1 || gy == 0 || gy == h - 1) {
                        outBuf[ry * w + x] = center;
                        continue;
                    }
                    int top = in[(inY - 1) * w + x];
                    int bottom = in[(inY + 1) * w + x];
                    int left = in[inY * w + x - 1];
                    int right = in[inY * w + x + 1];
                    int a = (center >>> 24) & 0xFF;
                    int r = sharpChannel((center >> 16) & 0xFF, (top >> 16) & 0xFF, (bottom >> 16) & 0xFF, (left >> 16) & 0xFF, (right >> 16) & 0xFF, amount);
                    int g = sharpChannel((center >> 8) & 0xFF, (top >> 8) & 0xFF, (bottom >> 8) & 0xFF, (left >> 8) & 0xFF, (right >> 8) & 0xFF, amount);
                    int b = sharpChannel(center & 0xFF, top & 0xFF, bottom & 0xFF, left & 0xFF, right & 0xFF, amount);
                    outBuf[ry * w + x] = (a << 24) | (r << 16) | (g << 8) | b;
                }
            }
            out.setPixels(outBuf, 0, w, 0, y, w, rows);
            y += rows;
        }
        return out;
    }

    private int sharpChannel(int center, int top, int bottom, int left, int right, float amount) {
        float lap = 4f * center - top - bottom - left - right;
        return clamp((int) (center + amount * lap));
    }

    // ---------------------------------------------------------------
    //  저장 / 삭제
    // ---------------------------------------------------------------
    private void saveBitmap(ContentResolver resolver, Bitmap bitmap, String displayName) throws IOException {
        ContentValues values = new ContentValues();
        values.put(MediaStore.Images.Media.DISPLAY_NAME, displayName);
        values.put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg");
        values.put(MediaStore.Images.Media.RELATIVE_PATH, OUTPUT_FOLDER);
        values.put(MediaStore.Images.Media.IS_PENDING, 1);
        Uri uri = resolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values);
        if (uri == null) {
            throw new IOException("MediaStore insert failed");
        }
        try (OutputStream stream = resolver.openOutputStream(uri)) {
            if (stream == null || !bitmap.compress(Bitmap.CompressFormat.JPEG, 95, stream)) {
                throw new IOException("Image compression failed");
            }
        }
        values.clear();
        values.put(MediaStore.Images.Media.IS_PENDING, 0);
        resolver.update(uri, values, null, null);
    }

    private void deleteOriginals(List<ImageItem> originals) {
        if (originals.isEmpty()) {
            return;
        }
        ContentResolver resolver = getContentResolver();
        // '모든 파일 접근'이 있으면 확인창 없이 실제 파일을 바로 삭제.
        if (Build.VERSION.SDK_INT >= 30 && Environment.isExternalStorageManager()) {
            int deleted = 0;
            for (ImageItem item : originals) {
                boolean ok = false;
                if (item.data != null) {
                    try {
                        ok = new File(item.data).delete();
                    } catch (Exception ignored) {
                    }
                }
                try {
                    resolver.delete(item.uri, null, null);
                    ok = true;
                } catch (Exception ignored) {
                }
                if (ok) {
                    deleted++;
                }
            }
            updateStatus(deleted + "개 원본을 자동 삭제했습니다.");
            return;
        }
        // 권한이 없으면 시스템 삭제 요청(한 번의 허용).
        List<Uri> uris = new ArrayList<>();
        for (ImageItem item : originals) {
            uris.add(item.uri);
        }
        try {
            if (Build.VERSION.SDK_INT >= 30) {
                PendingIntent request = MediaStore.createDeleteRequest(resolver, uris);
                startIntentSenderForResult(request.getIntentSender(), REQUEST_DELETE_IMAGES, null, 0, 0, 0);
            } else {
                for (Uri uri : uris) {
                    resolver.delete(uri, null, null);
                }
                updateStatus("원본 삭제까지 완료했습니다.");
            }
        } catch (Exception exception) {
            updateStatus("새 파일은 저장됐지만 원본 삭제에 실패했습니다.");
        }
    }

    // ---------------------------------------------------------------
    //  유틸
    // ---------------------------------------------------------------
    private int clamp(int value) {
        return value < 0 ? 0 : (value > 255 ? 255 : value);
    }

    private int clampInt(int value, int min, int max) {
        return value < min ? min : (value > max ? max : value);
    }

    private String timestampName(int index) {
        String stamp = new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date());
        return stamp + "_" + String.format(Locale.US, "%03d", index + 1);
    }

    private void updateProgress(int index, int total, String message) {
        runOnUiThread(() -> {
            progressBar.setProgress((int) (index * 100f / Math.max(1, total)));
            updateStatus(message);
        });
    }

    private void finishOnUi(String message, int progress) {
        runOnUiThread(() -> {
            progressBar.setProgress(progress);
            setControlsEnabled(true);
            updateStatus(message);
        });
    }

    private void setControlsEnabled(boolean enabled) {
        cropButton.setEnabled(enabled);
        keywordButton.setEnabled(enabled);
        blogWriterButton.setEnabled(enabled);
    }

    private void updateStatus(String message) {
        statusText.setText(message);
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    private static class ImageItem {
        final Uri uri;
        final String displayName;
        final String data;

        ImageItem(Uri uri, String displayName, String data) {
            this.uri = uri;
            this.displayName = displayName;
            this.data = data;
        }
    }
}
