package com.dicacros.picture;

import android.content.Context;
import android.content.SharedPreferences;
import android.webkit.CookieManager;
import android.webkit.ValueCallback;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 네이버 계정(macdcross / dicajohn + 사용자가 직접 입력한 임의 아이디)을 쿠키 스냅샷으로 관리한다.
 *
 * 하나의 WebView CookieManager 는 한 번에 한 네이버 세션만 들고 있으므로,
 * 계정 전환 시 (1) 현재 세션 쿠키 저장 → (2) 전체 쿠키 삭제 → (3) 대상 계정 쿠키 복원
 * 순으로 처리해 여러 로그인을 모두 유지한다. (nfriendcl / nclaude 와 동일한 방식)
 */
final class NaverAccounts {

    /** 토글 버튼 기본 계정. IDS[0] 이 앱 시작 시 기본 선택. */
    static final String[] IDS = {"macdcross", "dicajohn"};

    /** 첫 화면에서 바로 네이버 로그인 → 완료 시 현재 계정 세션 자동 저장. */
    static final String LOGIN_URL =
            "https://nid.naver.com/nidlogin.login?mode=form&url=https%3A%2F%2Fblog.naver.com";

    private static final String PREFS = "picture_naver_accounts";
    private static final String[] HOSTS = {
            "https://naver.com",
            "https://www.naver.com",
            "https://nid.naver.com",
            "https://blog.naver.com",
            "https://m.blog.naver.com"
    };

    private NaverAccounts() {
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** 현재 CookieManager 의 네이버 쿠키를 "n=v; n2=v2" 문자열로 병합. */
    static String snapshotCurrent() {
        CookieManager cm = CookieManager.getInstance();
        Map<String, String> map = new LinkedHashMap<>();
        for (String h : HOSTS) {
            String ck = cm.getCookie(h);
            if (ck == null) {
                continue;
            }
            for (String pair : ck.split(";")) {
                String p = pair.trim();
                int eq = p.indexOf('=');
                if (eq > 0) {
                    map.put(p.substring(0, eq), p.substring(eq + 1));
                }
            }
        }
        StringBuilder sb = new StringBuilder();
        for (Map.Entry<String, String> e : map.entrySet()) {
            if (sb.length() > 0) {
                sb.append("; ");
            }
            sb.append(e.getKey()).append('=').append(e.getValue());
        }
        return sb.toString();
    }

    /** NID 인증 쿠키 존재 여부로 로그인 상태 판단. */
    static boolean isLoggedIn() {
        String snap = snapshotCurrent();
        return snap.contains("NID_SES") || snap.contains("NID_AUT");
    }

    static void saveCurrentFor(Context ctx, String id) {
        String snap = snapshotCurrent();
        if (!snap.isEmpty() && (snap.contains("NID_SES") || snap.contains("NID_AUT"))) {
            prefs(ctx).edit().putString("ck_" + id, snap).apply();
        }
    }

    static String load(Context ctx, String id) {
        return prefs(ctx).getString("ck_" + id, null);
    }

    static boolean hasSession(Context ctx, String id) {
        String s = load(ctx, id);
        return s != null && !s.isEmpty();
    }

    interface Applied {
        void onApplied(boolean hadSession);
    }

    /**
     * 대상 계정으로 전환: 전체 쿠키 삭제 후 저장된 쿠키 복원.
     * removeAllCookies 콜백이 UI 스레드로 돌아오므로 done 도 UI 스레드에서 호출된다.
     */
    static void applyTo(final Context ctx, final String id, final Applied done) {
        final CookieManager cm = CookieManager.getInstance();
        cm.removeAllCookies(new ValueCallback<Boolean>() {
            @Override
            public void onReceiveValue(Boolean value) {
                String snap = load(ctx, id);
                boolean had = snap != null && !snap.isEmpty();
                if (had) {
                    for (String pair : snap.split(";")) {
                        String p = pair.trim();
                        if (p.isEmpty()) {
                            continue;
                        }
                        cm.setCookie("https://www.naver.com", p + "; Domain=.naver.com; Path=/; Secure");
                    }
                }
                cm.flush();
                if (done != null) {
                    done.onApplied(had);
                }
            }
        });
    }
}
