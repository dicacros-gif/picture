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
import android.provider.MediaStore;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;

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
 * Picture Cleaner — 오늘 캡처한 스크린샷을 일괄 처리한다.
 *
 * 버튼 1 (CROP_CONTENT): 글자/검은색/단색 여백을 제외한 실제 이미지 영역만 자동 크롭한다.
 *   캡처 메타데이터(EXIF·출처)는 비트맵을 새 JPEG로 재인코딩하면서 완전히 제거되고,
 *   결과가 작으면 해상도를 키운 뒤 새 파일로 저장하고 원본은 삭제한다.
 *
 * 버튼 2 (REMASTER): 갤럭시 '리마스터'처럼 대비/감마/채도/선명도를 보정하고 해상도를 키운다.
 *   원본은 삭제하고 개선된 사진을 새 이름으로 저장한다.
 *
 * 대용량 사진에서도 OOM 없이 동작하도록 분석은 축소본에서 수행하고,
 * 픽셀 보정은 가로 스트립 단위로 처리해 메모리 사용을 수 MB 이내로 제한한다.
 */
public class MainActivity extends Activity {
    private static final int REQUEST_READ_IMAGES = 1001;
    private static final int REQUEST_DELETE_IMAGES = 1002;
    private static final String OUTPUT_FOLDER = "Pictures/PictureCleaner";

    // 분석(경계 탐지·히스토그램)은 이 정도 크기로 축소한 뒤 수행한다.
    private static final int ANALYSIS_LONG_SIDE = 900;
    // 픽셀 보정 스트립 높이(메모리 상한 = width * STRIP_ROWS ints).
    private static final int STRIP_ROWS = 256;
    // 디코딩 시 이 화소 수를 넘으면 절반씩 다운샘플해 OOM을 방지한다.
    private static final int MAX_DECODE_PIXELS = 32 * 1024 * 1024;
    // 크롭 결과 목표 장변 / 리마스터 목표 장변 / 절대 상한.
    private static final int CROP_TARGET_LONG = 1600;
    private static final int REMASTER_TARGET_LONG = 2400;
    private static final int MAX_LONG_SIDE = 4096;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private TextView statusText;
    private ProgressBar progressBar;
    private Button cropButton;
    private Button remasterButton;
    private Button blogWriterButton;
    private CheckBox deleteOriginalCheck;
    private ProcessMode pendingMode;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(createContentView());
        updateStatus("오늘 캡처한 이미지를 처리할 준비가 됐습니다.");
    }

    @Override
    protected void onDestroy() {
        executor.shutdownNow();
        super.onDestroy();
    }

    private View createContentView() {
        ScrollView scrollView = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(24), dp(32), dp(24), dp(24));
        root.setBackgroundColor(Color.rgb(247, 248, 250));
        scrollView.addView(root);

        TextView title = new TextView(this);
        title.setText("Picture Cleaner");
        title.setTextColor(Color.rgb(16, 24, 40));
        title.setTextSize(28);
        title.setGravity(Gravity.START);
        title.setTypeface(null, 1);
        root.addView(title, new LinearLayout.LayoutParams(-1, -2));

        TextView subtitle = new TextView(this);
        subtitle.setText("오늘 찍은 스크린샷을 순서대로 처리합니다.");
        subtitle.setTextColor(Color.rgb(102, 112, 133));
        subtitle.setTextSize(15);
        LinearLayout.LayoutParams subtitleParams = new LinearLayout.LayoutParams(-1, -2);
        subtitleParams.setMargins(0, dp(8), 0, dp(24));
        root.addView(subtitle, subtitleParams);

        cropButton = createPrimaryButton("① 이미지만 자동 크롭 (글자·검은색 제거)");
        cropButton.setOnClickListener(v -> startProcessing(ProcessMode.CROP_CONTENT));
        root.addView(cropButton);

        remasterButton = createPrimaryButton("② 리마스터 + 해상도 개선");
        remasterButton.setOnClickListener(v -> startProcessing(ProcessMode.REMASTER));
        LinearLayout.LayoutParams remasterParams = new LinearLayout.LayoutParams(-1, dp(54));
        remasterParams.setMargins(0, dp(12), 0, dp(6));
        remasterButton.setLayoutParams(remasterParams);
        root.addView(remasterButton);

        deleteOriginalCheck = new CheckBox(this);
        deleteOriginalCheck.setText("처리 후 원본 삭제");
        deleteOriginalCheck.setChecked(true);
        deleteOriginalCheck.setTextColor(Color.rgb(52, 64, 84));
        LinearLayout.LayoutParams delParams = new LinearLayout.LayoutParams(-1, -2);
        delParams.setMargins(0, 0, 0, dp(14));
        deleteOriginalCheck.setLayoutParams(delParams);
        root.addView(deleteOriginalCheck);

        blogWriterButton = createPrimaryButton("③ 네이버 블로그 글쓰기 자동화");
        blogWriterButton.setBackgroundColor(Color.rgb(23, 78, 166));
        blogWriterButton.setOnClickListener(v -> startActivity(new Intent(this, BlogWriterActivity.class)));
        root.addView(blogWriterButton);

        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        progressBar.setProgress(0);
        LinearLayout.LayoutParams progressParams = new LinearLayout.LayoutParams(-1, dp(12));
        progressParams.setMargins(0, dp(6), 0, 0);
        root.addView(progressBar, progressParams);

        statusText = new TextView(this);
        statusText.setTextColor(Color.rgb(52, 64, 84));
        statusText.setTextSize(14);
        LinearLayout.LayoutParams statusParams = new LinearLayout.LayoutParams(-1, -2);
        statusParams.setMargins(0, dp(18), 0, 0);
        root.addView(statusText, statusParams);

        return scrollView;
    }

    private Button createPrimaryButton(String text) {
        Button button = new Button(this);
        button.setAllCaps(false);
        button.setText(text);
        button.setTextSize(16);
        button.setTextColor(Color.WHITE);
        button.setBackgroundColor(Color.rgb(47, 111, 237));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(54));
        params.setMargins(0, 0, 0, dp(12));
        button.setLayoutParams(params);
        return button;
    }

    private void startProcessing(ProcessMode mode) {
        if (!hasImagePermission()) {
            pendingMode = mode;
            requestImagePermission();
            return;
        }
        setControlsEnabled(false);
        progressBar.setProgress(0);
        updateStatus("오늘 스크린샷을 찾는 중입니다...");
        boolean deleteOriginals = deleteOriginalCheck.isChecked();
        executor.execute(() -> processTodayScreenshots(mode, deleteOriginals));
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

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQUEST_READ_IMAGES && grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            ProcessMode mode = pendingMode;
            pendingMode = null;
            if (mode != null) {
                startProcessing(mode);
            } else {
                updateStatus("권한이 허용됐습니다. 원하는 버튼을 눌러 주세요.");
            }
        } else if (requestCode == REQUEST_READ_IMAGES) {
            pendingMode = null;
            updateStatus("사진 읽기 권한이 필요합니다.");
        }
    }

    private void processTodayScreenshots(ProcessMode mode, boolean deleteOriginals) {
        ContentResolver resolver = getContentResolver();
        List<ImageItem> items = queryTodayScreenshots(resolver);
        if (items.isEmpty()) {
            finishOnUi("오늘 캡처한 스크린샷을 찾지 못했습니다.", 0);
            return;
        }

        List<Uri> originalsToDelete = new ArrayList<>();
        int savedCount = 0;
        int failedCount = 0;
        for (int index = 0; index < items.size(); index++) {
            ImageItem item = items.get(index);
            updateProgress(index, items.size(), item.displayName + " 처리 중...");
            Bitmap source = null;
            Bitmap output = null;
            try {
                source = decodeBitmap(resolver, item.uri);
                if (source == null) {
                    failedCount++;
                    continue;
                }
                Bitmap processed;
                int target;
                if (mode == ProcessMode.CROP_CONTENT) {
                    processed = cropContent(source);
                    target = CROP_TARGET_LONG;
                } else {
                    processed = remaster(source);
                    target = REMASTER_TARGET_LONG;
                }
                output = upscaleIfSmall(processed, target, MAX_LONG_SIDE);
                // 크롭/리마스터 중간 비트맵이 업스케일로 대체됐으면 즉시 회수.
                if (processed != output && processed != source) {
                    processed.recycle();
                }
                String prefix = mode == ProcessMode.CROP_CONTENT ? "cropped_" : "remastered_";
                saveBitmap(resolver, output, prefix + timestampName(index) + ".jpg");
                originalsToDelete.add(item.uri);
                savedCount++;
            } catch (Throwable throwable) {
                failedCount++;
            } finally {
                if (output != null && output != source) {
                    output.recycle();
                }
                if (source != null) {
                    source.recycle();
                }
            }
        }

        int finalSavedCount = savedCount;
        int finalFailedCount = failedCount;
        runOnUiThread(() -> {
            progressBar.setProgress(100);
            setControlsEnabled(true);
            String message = finalSavedCount + "개 저장 완료";
            if (finalFailedCount > 0) {
                message += ", " + finalFailedCount + "개 실패";
            }
            if (deleteOriginals && !originalsToDelete.isEmpty()) {
                updateStatus(message + ". 원본 삭제 확인 창이 뜨면 허용해 주세요.");
                requestDeleteOriginals(originalsToDelete);
            } else {
                updateStatus(message + ". 원본은 유지했습니다.");
            }
        });
    }

    private List<ImageItem> queryTodayScreenshots(ContentResolver resolver) {
        List<ImageItem> items = new ArrayList<>();
        Uri collection = MediaStore.Images.Media.EXTERNAL_CONTENT_URI;
        String[] projection = {
                MediaStore.Images.Media._ID,
                MediaStore.Images.Media.DISPLAY_NAME,
                MediaStore.Images.Media.DATE_ADDED,
                MediaStore.Images.Media.RELATIVE_PATH
        };

        Calendar start = Calendar.getInstance();
        start.set(Calendar.HOUR_OF_DAY, 0);
        start.set(Calendar.MINUTE, 0);
        start.set(Calendar.SECOND, 0);
        start.set(Calendar.MILLISECOND, 0);
        long startSeconds = start.getTimeInMillis() / 1000L;

        // 오늘 추가됐고, 파일명 또는 경로가 스크린샷인 항목만. 이미 처리한 결과 폴더는 제외.
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
            while (cursor.moveToNext()) {
                long id = cursor.getLong(idColumn);
                String displayName = cursor.getString(nameColumn);
                Uri uri = ContentUris.withAppendedId(collection, id);
                items.add(new ImageItem(uri, displayName));
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
        while (pixels / (sample * sample) > MAX_DECODE_PIXELS) {
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
    //  크롭: 실제 이미지 영역만 검출
    // ---------------------------------------------------------------

    private Bitmap cropContent(Bitmap source) {
        Rect bounds = detectContentBounds(source);
        if (bounds.width() <= 0 || bounds.height() <= 0
                || (bounds.width() == source.getWidth() && bounds.height() == source.getHeight())) {
            return source;
        }
        return Bitmap.createBitmap(source, bounds.left, bounds.top, bounds.width(), bounds.height());
    }

    /**
     * 축소본에서 활동도(채도 + 국부 에지)를 계산해, 단색/검은색 여백과 얇은 글자 띠를 배제하고
     * 가장 큰 연속 콘텐츠 밴드를 실제 좌표로 환산해 돌려준다.
     */
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

        // 픽셀별 콘텐츠 여부: 채도가 있거나(색이 있는 사진) 국부 에지가 뚜렷한 곳.
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
                // 순수 검정/순수 흰색 평면은 콘텐츠에서 제외.
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
        // 얇은 글자 한 줄이 밴드를 끊지 않도록 세로로 약하게 스무딩.
        boolean[] rowActive = smoothActive(rowScore, Math.max(1, aw / 12), 1);
        int[] vBand = longestRun(rowActive);
        int top = vBand[0];
        int bottom = vBand[1];
        if (bottom - top < ah / 8) {
            // 뚜렷한 밴드가 없으면 원본 그대로.
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

        // 축소 좌표 → 실제 좌표 환산 + 약간의 여백.
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

    /** score[i]가 임계(최대의 threshFrac) 이상이면 활성, radius 만큼 팽창해 얇은 틈을 메운다. */
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

    /** active 배열에서 가장 긴 true 구간 [start, end). */
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
    //  리마스터: 대비/감마/채도/선명도 보정
    // ---------------------------------------------------------------

    private Bitmap remaster(Bitmap source) {
        int[] lowHigh = percentileLuminance(source);
        int low = lowHigh[0];
        int range = Math.max(1, lowHigh[1] - lowHigh[0]);
        Bitmap toned = applyToneStripwise(source, low, range);
        Bitmap sharpened = sharpenStripwise(toned, 0.7f);
        if (sharpened != toned) {
            toned.recycle();
        }
        return sharpened;
    }

    /** 축소본 휘도 히스토그램에서 0.5% / 99.5% 백분위를 대비 스트레치 기준으로 잡는다. */
    private int[] percentileLuminance(Bitmap fullBitmap) {
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
        int[] hist = new int[256];
        for (int c : px) {
            hist[lumOf(c)]++;
        }
        int total = px.length;
        int lowCut = (int) (total * 0.005);
        int highCut = (int) (total * 0.005);
        int low = 0, high = 255;
        int acc = 0;
        for (int i = 0; i < 256; i++) {
            acc += hist[i];
            if (acc > lowCut) {
                low = i;
                break;
            }
        }
        acc = 0;
        for (int i = 255; i >= 0; i--) {
            acc += hist[i];
            if (acc > highCut) {
                high = i;
                break;
            }
        }
        if (high - low < 8) {
            low = 0;
            high = 255;
        }
        return new int[]{low, high};
    }

    /** 픽셀별 대비 스트레치 + 대비 부스트 + 감마 + 채도 강화. 스트립 단위로 처리. */
    private Bitmap applyToneStripwise(Bitmap source, int low, int range) {
        int w = source.getWidth();
        int h = source.getHeight();
        Bitmap out = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
        int[] lut = new int[256];
        for (int v = 0; v < 256; v++) {
            int stretched = clamp((v - low) * 255 / range);
            int contrasted = clamp((stretched - 128) * 110 / 100 + 128);
            lut[v] = clamp((int) (Math.pow(contrasted / 255.0, 0.90) * 255));
        }
        final float sat = 1.18f;
        int y = 0;
        while (y < h) {
            int rows = Math.min(STRIP_ROWS, h - y);
            int[] buf = new int[w * rows];
            source.getPixels(buf, 0, w, 0, y, w, rows);
            for (int i = 0; i < buf.length; i++) {
                int c = buf[i];
                int a = (c >>> 24) & 0xFF;
                int r = lut[(c >> 16) & 0xFF];
                int g = lut[(c >> 8) & 0xFF];
                int b = lut[c & 0xFF];
                int gray = (r * 299 + g * 587 + b * 114) / 1000;
                r = clamp((int) (gray + (r - gray) * sat));
                g = clamp((int) (gray + (g - gray) * sat));
                b = clamp((int) (gray + (b - gray) * sat));
                buf[i] = (a << 24) | (r << 16) | (g << 8) | b;
            }
            out.setPixels(buf, 0, w, 0, y, w, rows);
            y += rows;
        }
        return out;
    }

    /** 언샤프 마스크(3x3 라플라시안). 위/아래 1행 헤일로를 포함한 스트립으로 처리. */
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

    private Bitmap upscaleIfSmall(Bitmap source, int targetLongSide, int maxLongSide) {
        int longSide = Math.max(source.getWidth(), source.getHeight());
        if (longSide >= targetLongSide) {
            return source;
        }
        float scale = Math.min(maxLongSide / (float) longSide, targetLongSide / (float) longSide);
        int newW = Math.round(source.getWidth() * scale);
        int newH = Math.round(source.getHeight() * scale);
        Bitmap output = Bitmap.createScaledBitmap(source, Math.max(1, newW), Math.max(1, newH), true);
        return output;
    }

    private void saveBitmap(ContentResolver resolver, Bitmap bitmap, String displayName) throws IOException {
        // Bitmap → 새 JPEG 재인코딩이므로 원본 EXIF/캡처 출처 메타데이터는 남지 않는다.
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

    private void requestDeleteOriginals(List<Uri> originals) {
        if (originals.isEmpty()) {
            return;
        }
        try {
            if (Build.VERSION.SDK_INT >= 30) {
                PendingIntent request = MediaStore.createDeleteRequest(getContentResolver(), originals);
                startIntentSenderForResult(request.getIntentSender(), REQUEST_DELETE_IMAGES, null, 0, 0, 0);
            } else {
                for (Uri uri : originals) {
                    getContentResolver().delete(uri, null, null);
                }
                updateStatus("새 파일 저장과 원본 삭제가 완료됐습니다.");
            }
        } catch (Exception exception) {
            updateStatus("새 파일은 저장됐지만 원본 삭제 확인을 열지 못했습니다.");
        }
    }

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
        remasterButton.setEnabled(enabled);
        blogWriterButton.setEnabled(enabled);
    }

    private void updateStatus(String message) {
        statusText.setText(message);
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    private enum ProcessMode {
        CROP_CONTENT,
        REMASTER
    }

    private static class ImageItem {
        final Uri uri;
        final String displayName;

        ImageItem(Uri uri, String displayName) {
            this.uri = uri;
            this.displayName = displayName;
        }
    }
}
