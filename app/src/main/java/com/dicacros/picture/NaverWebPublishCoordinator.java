package com.dicacros.picture;

import android.content.Context;
import android.net.Uri;
import android.webkit.ValueCallback;
import android.webkit.WebView;

import java.util.Collections;
import java.util.List;

final class NaverWebPublishCoordinator {

    interface Listener {
        void onProgress(int progress, String message);

        void onFinished(boolean success, String message);
    }

    private static final int MAX_EDITOR_ATTEMPTS = 30;
    private static final int MAX_FILL_ATTEMPTS = 6;
    private static final int MAX_IMAGE_ATTEMPTS = 25;
    private static final int MAX_PUBLISH_ATTEMPTS = 12;

    private final Context context;
    private final WebView web;
    private final Listener listener;

    private int token;
    private boolean running;
    private boolean imagePickerRequested;
    private int deliveredImages = -1;
    private int imageBaseline;
    private int publishClicks;
    private boolean autoPublish;
    private String title = "";
    private String body = "";
    private List<Uri> images = Collections.emptyList();

    NaverWebPublishCoordinator(Context context, WebView web, Listener listener) {
        this.context = context.getApplicationContext();
        this.web = web;
        this.listener = listener;
    }

    boolean isRunning() {
        return running;
    }

    void start(String content, boolean useImages, boolean publish) {
        cancel();
        String[] parts = NaverPublisher.splitTitleBody(content);
        title = parts[0];
        body = parts[1];
        autoPublish = publish;
        images = useImages
                ? ProcessedImageStore.todayImages(context, 12)
                : Collections.emptyList();
        running = true;
        int currentToken = ++token;
        progress(55, "네이버 편집기가 준비되기를 기다립니다.");
        waitForEditor(currentToken, 0);
    }

    void cancel() {
        token++;
        running = false;
        imagePickerRequested = false;
        deliveredImages = -1;
        publishClicks = 0;
    }

    boolean onShowFileChooser(ValueCallback<Uri[]> callback) {
        if (!running || !imagePickerRequested || callback == null) {
            return false;
        }
        imagePickerRequested = false;
        deliveredImages = images.size();
        callback.onReceiveValue(images.isEmpty()
                ? null : images.toArray(new Uri[0]));
        return true;
    }

    private void waitForEditor(int currentToken, int attempt) {
        if (!isActive(currentToken)) {
            return;
        }
        NaverPublisher.runState(web, raw -> {
            if (!isActive(currentToken)) {
                return;
            }
            NaverPublisher.State state = NaverPublisher.parseState(raw);
            if (state.loginRequired) {
                fail("네이버 로그인이 필요합니다. 선택한 계정으로 로그인한 뒤 다시 실행하세요.");
                return;
            }
            if (state.editorReady && state.bodyReady) {
                imageBaseline = state.imageCount;
                fillEditor(currentToken, 0);
                return;
            }
            if (attempt >= MAX_EDITOR_ATTEMPTS) {
                fail("네이버 글쓰기 편집기를 찾지 못했습니다. 로그인 상태와 글쓰기 화면을 확인하세요.");
                return;
            }
            progress(55, "네이버 편집기 로딩 " + (attempt + 1)
                    + "/" + MAX_EDITOR_ATTEMPTS);
            web.postDelayed(
                    () -> waitForEditor(currentToken, attempt + 1), 1000L);
        });
    }

    private void fillEditor(int currentToken, int attempt) {
        if (!isActive(currentToken)) {
            return;
        }
        progress(65, "제목과 본문을 순서대로 입력합니다.");
        NaverPublisher.runFill(web, title, body, raw -> {
            if (!isActive(currentToken)) {
                return;
            }
            NaverPublisher.Action action = NaverPublisher.parseAction(raw);
            if (action.titleFilled
                    && action.bodyFilled
                    && action.bodyLength > 0) {
                web.postDelayed(
                        () -> verifyFilledEditor(currentToken, attempt), 500L);
                return;
            }
            if (attempt >= MAX_FILL_ATTEMPTS) {
                fail("네이버 제목 또는 본문 입력을 확인하지 못했습니다"
                        + suffix(action.error) + ".");
                return;
            }
            web.postDelayed(
                    () -> fillEditor(currentToken, attempt + 1), 900L);
        });
    }

    private void verifyFilledEditor(int currentToken, int fillAttempt) {
        if (!isActive(currentToken)) {
            return;
        }
        NaverPublisher.runState(web, raw -> {
            if (!isActive(currentToken)) {
                return;
            }
            NaverPublisher.State state = NaverPublisher.parseState(raw);
            int requiredTitle = Math.min(8, title.trim().length());
            int requiredBody = Math.min(
                    120, Math.max(20, body.trim().length() / 4));
            if (state.titleLength >= requiredTitle
                    && state.bodyLength >= requiredBody) {
                if (images.isEmpty()) {
                    continueToPublish(currentToken);
                } else {
                    openImagePicker(currentToken);
                }
                return;
            }
            if (fillAttempt >= MAX_FILL_ATTEMPTS) {
                fail("네이버 편집기에서 제목·본문 반영을 확인하지 못했습니다.");
                return;
            }
            web.postDelayed(
                    () -> fillEditor(currentToken, fillAttempt + 1), 700L);
        });
    }

    private void openImagePicker(int currentToken) {
        if (!isActive(currentToken)) {
            return;
        }
        progress(75, "본문 중간에 정리된 사진을 삽입합니다.");
        deliveredImages = -1;
        imagePickerRequested = true;
        NaverPublisher.runOpenImagePicker(web, raw -> {
            if (!isActive(currentToken)) {
                return;
            }
            NaverPublisher.Action action = NaverPublisher.parseAction(raw);
            if (action.clicked.isEmpty()) {
                imagePickerRequested = false;
                fail("네이버 사진 버튼을 찾지 못했습니다"
                        + suffix(action.error) + ".");
                return;
            }
            web.postDelayed(
                    () -> waitForImages(currentToken, 0), 1000L);
        });
    }

    private void waitForImages(int currentToken, int attempt) {
        if (!isActive(currentToken)) {
            return;
        }
        if (deliveredImages == 0) {
            fail("오늘 정리된 사진을 네이버에 전달하지 못했습니다.");
            return;
        }
        NaverPublisher.runState(web, raw -> {
            if (!isActive(currentToken)) {
                return;
            }
            NaverPublisher.State state = NaverPublisher.parseState(raw);
            if (deliveredImages > 0
                    && state.imageCount > imageBaseline
                    && !state.busy) {
                progress(85, "사진 " + deliveredImages
                        + "개 삽입을 확인했습니다.");
                continueToPublish(currentToken);
                return;
            }
            if (attempt >= MAX_IMAGE_ATTEMPTS) {
                fail("사진 파일은 전달했지만 본문 삽입 완료를 확인하지 못했습니다. "
                        + "사진을 확인한 뒤 수동 발행해 주세요.");
                return;
            }
            web.postDelayed(
                    () -> waitForImages(currentToken, attempt + 1), 1000L);
        });
    }

    private void continueToPublish(int currentToken) {
        if (!isActive(currentToken)) {
            return;
        }
        if (!autoPublish) {
            succeed(images.isEmpty()
                    ? "제목·본문 입력을 확인했습니다. 발행 버튼은 직접 눌러 주세요."
                    : "제목·본문·사진 입력을 확인했습니다. 발행 버튼은 직접 눌러 주세요.");
            return;
        }
        publishClicks = 0;
        clickPublish(currentToken, 0);
    }

    private void clickPublish(int currentToken, int attempt) {
        if (!isActive(currentToken)) {
            return;
        }
        progress(92, publishClicks == 0
                ? "발행 버튼을 누릅니다."
                : "최종 발행을 확인합니다.");
        NaverPublisher.runPublish(web, raw -> {
            if (!isActive(currentToken)) {
                return;
            }
            NaverPublisher.Action action = NaverPublisher.parseAction(raw);
            if (!action.clicked.isEmpty()) {
                publishClicks++;
                web.postDelayed(
                        () -> verifyPublished(currentToken, attempt), 1500L);
                return;
            }
            if (attempt >= MAX_PUBLISH_ATTEMPTS) {
                fail("발행 버튼을 찾지 못했습니다"
                        + suffix(action.error) + ".");
                return;
            }
            web.postDelayed(
                    () -> clickPublish(currentToken, attempt + 1), 1000L);
        });
    }

    private void verifyPublished(int currentToken, int attempt) {
        if (!isActive(currentToken)) {
            return;
        }
        NaverPublisher.runState(web, raw -> {
            if (!isActive(currentToken)) {
                return;
            }
            NaverPublisher.State state = NaverPublisher.parseState(raw);
            if (state.success
                    || (!state.editorReady && !state.publishReady
                    && !state.loginRequired)) {
                succeed("네이버 블로그 발행 완료를 확인했습니다.");
                return;
            }
            if (!state.busy && state.publishReady && publishClicks < 3) {
                clickPublish(currentToken, attempt + 1);
                return;
            }
            if (attempt >= MAX_PUBLISH_ATTEMPTS) {
                fail("발행 버튼은 눌렀지만 완료 화면을 확인하지 못했습니다. "
                        + "네이버 화면에서 발행 상태를 확인해 주세요.");
                return;
            }
            web.postDelayed(
                    () -> verifyPublished(currentToken, attempt + 1), 1200L);
        });
    }

    private boolean isActive(int currentToken) {
        return running && currentToken == token;
    }

    private void progress(int value, String message) {
        if (listener != null) {
            listener.onProgress(value, message);
        }
    }

    private void succeed(String message) {
        if (!running) {
            return;
        }
        running = false;
        imagePickerRequested = false;
        if (listener != null) {
            listener.onFinished(true, message);
        }
    }

    private void fail(String message) {
        if (!running) {
            return;
        }
        running = false;
        imagePickerRequested = false;
        if (listener != null) {
            listener.onFinished(false, message);
        }
    }

    private String suffix(String error) {
        return error == null || error.trim().isEmpty()
                ? "" : ": " + error.trim();
    }
}
