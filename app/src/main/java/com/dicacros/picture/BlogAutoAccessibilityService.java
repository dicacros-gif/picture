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

import java.util.List;

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
        handler.postDelayed(this::tapBottomCenter, 1500);
        handler.postDelayed(() -> clickByTexts("글쓰기", "새 글", "글 작성"), 2600);
        handler.postDelayed(this::tapCenter, 4200);
        handler.postDelayed(() -> setTextOnFocused(content), 5200);
        if (publish) {
            handler.postDelayed(() -> clickByTexts("발행", "등록", "다음", "확인"), 7200);
            handler.postDelayed(() -> clickByTexts("발행", "등록", "확인", "완료"), 9200);
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
}
