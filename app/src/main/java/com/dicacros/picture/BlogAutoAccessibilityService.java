package com.dicacros.picture;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Path;
import android.graphics.Point;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Display;
import android.view.WindowManager;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * 네이버 블로그 앱 화면 위에서 제스처/클릭을 대신 수행하는 접근성 서비스.
 *
 * WebView 자동화가 막혔을 때의 대체 경로: 사용자가 설정에서 이 서비스를 켜면
 * 앱은 글쓰기 화면 하단 중앙을 눌러 본문 편집을 시작하고, 생성한 글을 입력한 뒤,
 * 발행 버튼까지 눌러 준다. 접근성 특성상 화면이 켜져 있을 때만 안정적으로 동작한다.
 */
public class BlogAutoAccessibilityService extends AccessibilityService {

    private static BlogAutoAccessibilityService instance;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private int chatAutomationToken;
    private int naverAutomationToken;

    interface ChatResultCallback {
        void onResult(String text, String error);
    }

    interface NaverPostCallback {
        void onResult(boolean success, String message);
    }

    static boolean isRunning() {
        return instance != null;
    }

    static BlogAutoAccessibilityService get() {
        return instance;
    }

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        instance = this;
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        // 이벤트 구독은 활성 창 접근을 위해 필요하지만 별도 처리는 하지 않는다.
    }

    @Override
    public void onInterrupt() {
    }

    @Override
    public void onDestroy() {
        chatAutomationToken++;
        naverAutomationToken++;
        handler.removeCallbacksAndMessages(null);
        if (instance == this) {
            instance = null;
        }
        super.onDestroy();
    }

    // ---------------------------------------------------------------
    //  공개 동작
    // ---------------------------------------------------------------

    /** 글쓰기 화면 하단 중앙을 탭한다(본문 편집 진입 유도). */
    void tapBottomCenter() {
        Point size = realSize();
        tap(size.x / 2f, size.y * 0.92f);
    }

    void tapCenter() {
        Point size = realSize();
        tap(size.x / 2f, size.y * 0.5f);
    }

    /**
     * 네이버 앱 글쓰기 자동 시퀀스: 하단 중앙 탭 → 본문 입력 → (옵션) 발행 버튼 탭.
     * 각 단계 사이에 지연을 둬 화면 전환을 기다린다.
     */
    void automateNaverPost(final String content, final boolean publish) {
        automateNaverPost(content, publish, false);
    }

    void automateNaverPost(final String content, final boolean publish,
                           final boolean sharedImages) {
        String[] parts = NaverPublisher.splitTitleBody(content);
        automateNaverPost(
                parts[0], parts[1], publish, sharedImages, null);
    }

    void automateNaverPost(
            final String title, final String body, final boolean publish,
            final boolean sharedImages, final NaverPostCallback callback) {
        final int token = ++naverAutomationToken;
        handler.postDelayed(() -> prepareNaverEditor(
                token, title, body, publish, sharedImages, callback, 0), 900L);
    }

    private void prepareNaverEditor(
            int token, String title, String body, boolean publish,
            boolean sharedImages, NaverPostCallback callback, int attempt) {
        if (token != naverAutomationToken) {
            return;
        }
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (!isNaverWindow(root)) {
            if (attempt < 30) {
                handler.postDelayed(() -> prepareNaverEditor(
                        token, title, body, publish, sharedImages,
                        callback, attempt + 1), 1000L);
            } else {
                finishNaver(token, callback, false,
                        "네이버 블로그 앱 화면을 확인할 수 없습니다.");
            }
            return;
        }

        EditorFields fields = findEditorFields(root);
        if (fields.title != null && fields.body != null) {
            boolean titleSet = setText(fields.title, title);
            boolean bodySet = setText(fields.body, body);
            if (titleSet && bodySet) {
                handler.postDelayed(() -> verifyNaverEditor(
                        token, title, body, publish, callback, 0), 800L);
                return;
            }
        }

        if (attempt >= 30) {
            finishNaver(token, callback, false,
                    "네이버 제목 또는 본문 입력란을 찾지 못했습니다.");
            return;
        }
        if (sharedImages) {
            if (attempt == 1 || attempt == 5) {
                clickExactByTexts("다음", "완료", "확인", "첨부");
            }
        } else if (attempt == 1 || attempt == 5) {
            clickExactByTexts("글쓰기", "새 글", "글 작성");
        }
        if (attempt == 3 || attempt == 8) {
            tapCenter();
        }
        handler.postDelayed(() -> prepareNaverEditor(
                token, title, body, publish, sharedImages,
                callback, attempt + 1), 1000L);
    }

    private void verifyNaverEditor(
            int token, String title, String body, boolean publish,
            NaverPostCallback callback, int attempt) {
        if (token != naverAutomationToken) {
            return;
        }
        AccessibilityNodeInfo root = getRootInActiveWindow();
        EditorFields fields = findEditorFields(root);
        boolean titleReady = fields.title != null
                && nodeContains(fields.title, title);
        boolean bodyReady = fields.body != null
                && nodeContains(fields.body, body);
        if (titleReady && bodyReady) {
            if (!publish) {
                finishNaver(token, callback, true,
                        "네이버 앱 제목·본문 입력을 확인했습니다. 발행 버튼은 직접 눌러 주세요.");
            } else {
                clickNaverPublish(token, callback, 0, 0);
            }
            return;
        }
        if (attempt >= 8) {
            finishNaver(token, callback, false,
                    "네이버 앱에 입력은 시도했지만 제목·본문 반영을 확인하지 못했습니다.");
            return;
        }
        if (fields.title != null && !titleReady) {
            setText(fields.title, title);
        }
        if (fields.body != null && !bodyReady) {
            setText(fields.body, body);
        }
        handler.postDelayed(() -> verifyNaverEditor(
                token, title, body, publish, callback, attempt + 1), 700L);
    }

    private void clickNaverPublish(
            int token, NaverPostCallback callback, int clicks, int attempt) {
        if (token != naverAutomationToken) {
            return;
        }
        if (clickExactByTexts("발행", "등록", "게시", "완료", "확인")) {
            handler.postDelayed(() -> verifyNaverPublished(
                    token, callback, clicks + 1, attempt), 1300L);
            return;
        }
        if (attempt >= 12) {
            finishNaver(token, callback, false,
                    "네이버 앱 발행 버튼을 찾지 못했습니다.");
            return;
        }
        handler.postDelayed(() -> clickNaverPublish(
                token, callback, clicks, attempt + 1), 900L);
    }

    private void verifyNaverPublished(
            int token, NaverPostCallback callback, int clicks, int attempt) {
        if (token != naverAutomationToken) {
            return;
        }
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (containsAnyText(root,
                "발행되었습니다", "게시글이 등록", "글이 등록되었습니다")) {
            finishNaver(token, callback, true,
                    "네이버 앱 발행 완료를 확인했습니다.");
            return;
        }
        EditorFields fields = findEditorFields(root);
        boolean publishControl = containsAnyText(
                root, "발행", "등록", "게시", "완료", "확인");
        if (clicks > 0 && fields.title == null && fields.body == null
                && !publishControl && isNaverWindow(root)) {
            finishNaver(token, callback, true,
                    "네이버 앱 발행 후 편집기 종료를 확인했습니다.");
            return;
        }
        if (clicks < 3 && clickExactByTexts(
                "발행", "등록", "게시", "완료", "확인")) {
            handler.postDelayed(() -> verifyNaverPublished(
                    token, callback, clicks + 1, attempt + 1), 1300L);
            return;
        }
        if (attempt >= 12) {
            finishNaver(token, callback, false,
                    "발행 버튼은 눌렀지만 완료 상태를 확인하지 못했습니다.");
            return;
        }
        handler.postDelayed(() -> verifyNaverPublished(
                token, callback, clicks, attempt + 1), 1000L);
    }

    private void finishNaver(
            int token, NaverPostCallback callback,
            boolean success, String message) {
        if (token != naverAutomationToken) {
            return;
        }
        naverAutomationToken++;
        if (callback != null) {
            callback.onResult(success, message);
        }
    }

    void automateChatGpt(final String prompt, final ChatResultCallback callback) {
        final int token = ++chatAutomationToken;
        handler.postDelayed(
                () -> prepareChatGpt(token, prompt, callback, 0), 1000);
    }

    private void prepareChatGpt(int token, String prompt,
                                ChatResultCallback callback, int attempt) {
        if (token != chatAutomationToken) {
            return;
        }
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (!isChatGptWindow(root)) {
            if (attempt < 15) {
                handler.postDelayed(
                        () -> prepareChatGpt(token, prompt, callback, attempt + 1), 1000);
            } else {
                finishChatGpt(token, callback, "",
                        "ChatGPT 앱 화면을 확인할 수 없습니다.");
            }
            return;
        }
        AccessibilityNodeInfo editable = findEditable(root);
        if (editable == null || !setText(editable, prompt)) {
            if (attempt < 15) {
                handler.postDelayed(
                        () -> prepareChatGpt(token, prompt, callback, attempt + 1), 1000);
            } else {
                finishChatGpt(token, callback, "",
                        "ChatGPT 앱 입력창을 찾지 못했습니다.");
            }
            return;
        }
        handler.postDelayed(() -> {
            if (token != chatAutomationToken) {
                return;
            }
            if (!clickChatGptSend()) {
                Point size = realSize();
                tap(size.x * 0.91f, size.y * 0.92f);
            }
            pollChatGpt(token, prompt, callback,
                    System.currentTimeMillis() + 180_000L, "", 0);
        }, 900);
    }

    private void pollChatGpt(int token, String prompt,
                             ChatResultCallback callback, long deadline,
                             String previous, int stablePolls) {
        if (token != chatAutomationToken) {
            return;
        }
        if (System.currentTimeMillis() >= deadline) {
            finishChatGpt(token, callback, "",
                    "ChatGPT 앱 응답 대기 시간이 지났습니다.");
            return;
        }
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (!isChatGptWindow(root)) {
            handler.postDelayed(() -> pollChatGpt(
                    token, prompt, callback, deadline, previous, stablePolls), 2500);
            return;
        }
        String candidate = extractChatGptResponse(root, prompt);
        int nextStable = candidate.equals(previous) && !candidate.isEmpty()
                ? stablePolls + 1 : 0;
        if (candidate.length() >= 200 && !isChatGptBusy(root) && nextStable >= 2) {
            finishChatGpt(token, callback, candidate, "");
            return;
        }
        handler.postDelayed(() -> pollChatGpt(
                token, prompt, callback, deadline, candidate, nextStable), 2500);
    }

    private void finishChatGpt(int token, ChatResultCallback callback,
                               String text, String error) {
        if (token != chatAutomationToken) {
            return;
        }
        chatAutomationToken++;
        if (callback != null) {
            callback.onResult(text, error);
        }
    }

    boolean setTextOnFocused(String text) {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) {
            return false;
        }
        AccessibilityNodeInfo target = findEditable(root);
        if (target == null) {
            return false;
        }
        return setText(target, text);
    }

    private boolean setText(AccessibilityNodeInfo target, String text) {
        target.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
        Bundle args = new Bundle();
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        return target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
    }

    boolean clickByTexts(String... texts) {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) {
            return false;
        }
        for (String t : texts) {
            List<AccessibilityNodeInfo> nodes = root.findAccessibilityNodeInfosByText(t);
            if (nodes == null) {
                continue;
            }
            for (AccessibilityNodeInfo node : nodes) {
                AccessibilityNodeInfo clickable = node;
                while (clickable != null && !clickable.isClickable()) {
                    clickable = clickable.getParent();
                }
                if (clickable != null && clickable.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                    return true;
                }
            }
        }
        return false;
    }

    private boolean clickExactByTexts(String... texts) {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) {
            return false;
        }
        for (String text : texts) {
            List<AccessibilityNodeInfo> nodes =
                    root.findAccessibilityNodeInfosByText(text);
            if (nodes == null) {
                continue;
            }
            for (AccessibilityNodeInfo node : nodes) {
                String value = node.getText() == null
                        ? "" : node.getText().toString().trim();
                String description = node.getContentDescription() == null
                        ? "" : node.getContentDescription().toString().trim();
                if (!text.equals(value) && !text.equals(description)) {
                    continue;
                }
                AccessibilityNodeInfo clickable = node;
                while (clickable != null && !clickable.isClickable()) {
                    clickable = clickable.getParent();
                }
                if (clickable != null
                        && clickable.performAction(
                        AccessibilityNodeInfo.ACTION_CLICK)) {
                    return true;
                }
            }
        }
        return false;
    }

    private boolean clickChatGptSend() {
        return clickByDescriptions(
                "메시지 보내기", "프롬프트 보내기", "전송", "Send message",
                "Send prompt", "Send")
                || clickByTexts("보내기", "전송", "Send");
    }

    private boolean clickByDescriptions(String... descriptions) {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) {
            return false;
        }
        return clickByDescriptions(root, descriptions);
    }

    private boolean clickByDescriptions(
            AccessibilityNodeInfo node, String... descriptions) {
        if (node == null) {
            return false;
        }
        String description = node.getContentDescription() == null
                ? "" : node.getContentDescription().toString();
        for (String expected : descriptions) {
            if (!description.isEmpty()
                    && description.toLowerCase(Locale.ROOT)
                    .contains(expected.toLowerCase(Locale.ROOT))) {
                AccessibilityNodeInfo clickable = node;
                while (clickable != null && !clickable.isClickable()) {
                    clickable = clickable.getParent();
                }
                if (clickable != null
                        && clickable.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                    return true;
                }
            }
        }
        for (int index = 0; index < node.getChildCount(); index++) {
            if (clickByDescriptions(node.getChild(index), descriptions)) {
                return true;
            }
        }
        return false;
    }

    // ---------------------------------------------------------------
    //  내부
    // ---------------------------------------------------------------
    private AccessibilityNodeInfo findEditable(AccessibilityNodeInfo node) {
        if (node == null) {
            return null;
        }
        if (node.isEditable()) {
            return node;
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo found = findEditable(node.getChild(i));
            if (found != null) {
                return found;
            }
        }
        return null;
    }

    private EditorFields findEditorFields(AccessibilityNodeInfo root) {
        List<AccessibilityNodeInfo> editable = new ArrayList<>();
        collectEditable(root, editable);
        AccessibilityNodeInfo title = null;
        AccessibilityNodeInfo body = null;
        for (AccessibilityNodeInfo node : editable) {
            String label = nodeLabel(node).toLowerCase(Locale.ROOT);
            if (title == null && (label.contains("제목")
                    || label.contains("title"))) {
                title = node;
            } else if (body == null && (label.contains("본문")
                    || label.contains("내용")
                    || label.contains("content")
                    || label.contains("글을 입력"))) {
                body = node;
            }
        }
        if (editable.size() >= 2) {
            if (title == null) {
                title = editable.get(0);
            }
            if (body == null) {
                body = editable.get(editable.size() - 1);
            }
        }
        if (title == body) {
            body = null;
        }
        return new EditorFields(title, body);
    }

    private void collectEditable(
            AccessibilityNodeInfo node, List<AccessibilityNodeInfo> output) {
        if (node == null) {
            return;
        }
        if (node.isEditable()) {
            output.add(node);
        }
        for (int index = 0; index < node.getChildCount(); index++) {
            collectEditable(node.getChild(index), output);
        }
    }

    private String nodeLabel(AccessibilityNodeInfo node) {
        StringBuilder label = new StringBuilder();
        if (node.getHintText() != null) {
            label.append(node.getHintText()).append(' ');
        }
        if (node.getContentDescription() != null) {
            label.append(node.getContentDescription()).append(' ');
        }
        if (node.getText() != null) {
            label.append(node.getText()).append(' ');
        }
        if (node.getViewIdResourceName() != null) {
            label.append(node.getViewIdResourceName());
        }
        return label.toString();
    }

    private boolean nodeContains(AccessibilityNodeInfo node, String expected) {
        if (node == null || expected == null || expected.isEmpty()) {
            return false;
        }
        String actual = node.getText() == null
                ? "" : node.getText().toString();
        String marker = expected.length() > 40
                ? expected.substring(0, 40) : expected;
        return actual.equals(expected) || actual.contains(marker);
    }

    private boolean containsAnyText(
            AccessibilityNodeInfo node, String... expectedValues) {
        if (node == null) {
            return false;
        }
        String text = node.getText() == null
                ? "" : node.getText().toString();
        String description = node.getContentDescription() == null
                ? "" : node.getContentDescription().toString();
        for (String expected : expectedValues) {
            if (text.contains(expected) || description.contains(expected)) {
                return true;
            }
        }
        for (int index = 0; index < node.getChildCount(); index++) {
            if (containsAnyText(node.getChild(index), expectedValues)) {
                return true;
            }
        }
        return false;
    }

    private boolean isNaverWindow(AccessibilityNodeInfo root) {
        return root != null && root.getPackageName() != null
                && AutoConfig.NAVER_APP_PACKAGE.contentEquals(
                root.getPackageName());
    }

    private boolean isChatGptWindow(AccessibilityNodeInfo root) {
        return root != null && root.getPackageName() != null
                && AutoConfig.CHATGPT_APP_PACKAGE.contentEquals(root.getPackageName());
    }

    private boolean isChatGptBusy(AccessibilityNodeInfo root) {
        List<String> values = new ArrayList<>();
        collectNodeStrings(root, values, true);
        for (String value : values) {
            String lower = value.toLowerCase(Locale.ROOT);
            if (lower.contains("응답 중지") || lower.contains("생성 중지")
                    || lower.contains("stop generating")
                    || lower.contains("stop response")) {
                return true;
            }
        }
        return false;
    }

    private String extractChatGptResponse(
            AccessibilityNodeInfo root, String prompt) {
        List<String> values = new ArrayList<>();
        collectNodeStrings(root, values, false);
        int promptIndex = -1;
        String promptMarker = prompt.length() > 80
                ? prompt.substring(0, 80) : prompt;
        for (int index = 0; index < values.size(); index++) {
            String value = values.get(index);
            if (value.equals(prompt) || value.contains(promptMarker)) {
                promptIndex = index;
            }
        }
        Set<String> unique = new LinkedHashSet<>();
        int start = promptIndex >= 0 ? promptIndex + 1 : 0;
        for (int index = start; index < values.size(); index++) {
            String value = values.get(index).trim();
            if (value.length() < 2 || value.equals(prompt)
                    || value.contains(promptMarker) || isChatUiText(value)) {
                continue;
            }
            unique.add(value);
        }
        StringBuilder output = new StringBuilder();
        for (String value : unique) {
            if (output.length() > 0) {
                output.append('\n');
            }
            output.append(value);
        }
        if (output.length() >= 200) {
            return output.toString().trim();
        }
        String longest = "";
        for (String value : values) {
            if (!value.equals(prompt) && !value.contains(promptMarker)
                    && !isChatUiText(value) && value.length() > longest.length()) {
                longest = value;
            }
        }
        return longest.trim();
    }

    private void collectNodeStrings(AccessibilityNodeInfo node,
                                    List<String> output, boolean includeDescriptions) {
        if (node == null) {
            return;
        }
        if (node.getChildCount() == 0) {
            CharSequence text = node.getText();
            if (text != null && !text.toString().trim().isEmpty()) {
                output.add(text.toString().trim());
            }
            if (includeDescriptions) {
                CharSequence description = node.getContentDescription();
                if (description != null && !description.toString().trim().isEmpty()) {
                    output.add(description.toString().trim());
                }
            }
        }
        for (int index = 0; index < node.getChildCount(); index++) {
            collectNodeStrings(node.getChild(index), output, includeDescriptions);
        }
    }

    private boolean isChatUiText(String value) {
        String lower = value.toLowerCase(Locale.ROOT).trim();
        return lower.equals("chatgpt")
                || lower.equals("새 채팅")
                || lower.equals("new chat")
                || lower.equals("보내기")
                || lower.equals("전송")
                || lower.equals("send")
                || lower.equals("복사")
                || lower.equals("copy")
                || lower.equals("좋아요")
                || lower.equals("싫어요")
                || lower.equals("다시 생성")
                || lower.equals("regenerate")
                || lower.startsWith("무엇이든 물어")
                || lower.startsWith("message chatgpt");
    }

    private void tap(float x, float y) {
        Path path = new Path();
        path.moveTo(x, y);
        GestureDescription.Builder builder = new GestureDescription.Builder();
        builder.addStroke(new GestureDescription.StrokeDescription(path, 0, 80));
        dispatchGesture(builder.build(), null, null);
    }

    private Point realSize() {
        Point size = new Point();
        WindowManager wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        if (wm != null) {
            Display display = wm.getDefaultDisplay();
            display.getRealSize(size);
        }
        if (size.x == 0 || size.y == 0) {
            size.x = getResources().getDisplayMetrics().widthPixels;
            size.y = getResources().getDisplayMetrics().heightPixels;
        }
        return size;
    }

    private static final class EditorFields {
        final AccessibilityNodeInfo title;
        final AccessibilityNodeInfo body;

        EditorFields(
                AccessibilityNodeInfo title, AccessibilityNodeInfo body) {
            this.title = title;
            this.body = body;
        }
    }
}
