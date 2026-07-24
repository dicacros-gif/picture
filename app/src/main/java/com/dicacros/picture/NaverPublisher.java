package com.dicacros.picture;

import android.webkit.WebView;

import org.json.JSONObject;

/**
 * WebView 안에서 네이버 모바일 글쓰기 화면을 채우고 발행 버튼까지 누르는 최선 노력(best-effort) 자동화.
 *
 * 네이버 SmartEditor 구조는 수시로 바뀌므로 여러 선택자/버튼 텍스트를 폭넓게 시도한다.
 * 실패해도 초안이 클립보드에 있으므로 사용자가 수동 발행할 수 있다.
 */
final class NaverPublisher {

    private NaverPublisher() {
    }

    static String writeUrl(String blogId) {
        return "https://m.blog.naver.com/" + blogId + "?Redirect=Write";
    }

    /** 제목+본문을 에디터에 채우는 JS. 제목/본문 분리가 어려우면 본문 영역에 전체를 넣는다. */
    static String fillJs(String title, String body) {
        String t = JSONObject.quote(title == null ? "" : title);
        String b = JSONObject.quote(body == null ? "" : body);
        return "(function(){try{"
                + "var title=" + t + ";var body=" + b + ";"
                + "function fire(el){['input','keyup','change','blur'].forEach(function(ev){"
                + "el.dispatchEvent(new Event(ev,{bubbles:true}));});}"
                + "function setInput(el,v){if(!el)return false;"
                + "if(el.isContentEditable){el.focus();el.innerText=v;fire(el);return true;}"
                + "if('value' in el){el.focus();el.value=v;fire(el);return true;}return false;}"
                + "var tSel=['textarea[placeholder*=\"제목\"]','input[placeholder*=\"제목\"]',"
                + "'.se-title-text','[contenteditable][class*=title]','.htitle'];"
                + "var bSel=['[contenteditable=\"true\"]','.se-text-paragraph','textarea',"
                + "'.se-component-content','#editorArea','.editor'];"
                + "var okT=false;for(var i=0;i<tSel.length&&!okT;i++){okT=setInput(document.querySelector(tSel[i]),title);}"
                + "var okB=false;for(var j=0;j<bSel.length&&!okB;j++){var el=document.querySelector(bSel[j]);"
                + "if(el&&el.offsetHeight>40){okB=setInput(el,body);}}"
                + "if(!okB){var eds=document.querySelectorAll('[contenteditable=\"true\"]');"
                + "if(eds.length){okB=setInput(eds[eds.length-1],body);}}"
                + "return JSON.stringify({title:okT,body:okB});}catch(e){return JSON.stringify({error:String(e)});}})();";
    }

    /** 발행/등록/확인 계열 버튼을 눌러 최종 발행까지 진행하는 JS. */
    static final String PUBLISH_JS =
            "(function(){try{"
                    + "function clickByText(texts){var els=document.querySelectorAll('button,a,span,div[role=button]');"
                    + "for(var i=0;i<els.length;i++){var el=els[i];var t=(el.innerText||el.textContent||'').trim();"
                    + "for(var k=0;k<texts.length;k++){if(t===texts[k]||t.indexOf(texts[k])>=0){"
                    + "if(el.offsetHeight>0){el.click();return t;}}}}return null;}"
                    + "var a=clickByText(['발행']);"
                    + "if(!a){a=clickByText(['등록','확인','완료','게시']);}"
                    + "return JSON.stringify({clicked:a});}catch(e){return JSON.stringify({error:String(e)});}})();";

    interface StepResult {
        void onResult(String json);
    }

    static void runFill(WebView web, String title, String body, StepResult cb) {
        web.evaluateJavascript(fillJs(title, body), value -> {
            if (cb != null) {
                cb.onResult(value);
            }
        });
    }

    static void runPublish(WebView web, StepResult cb) {
        web.evaluateJavascript(PUBLISH_JS, value -> {
            if (cb != null) {
                cb.onResult(value);
            }
        });
    }

    /** 생성 결과에서 첫 줄을 제목, 나머지를 본문으로 분리. */
    static String[] splitTitleBody(String result) {
        if (result == null) {
            return new String[]{"", ""};
        }
        String trimmed = result.trim();
        int nl = trimmed.indexOf('\n');
        if (nl <= 0) {
            return new String[]{trimmed, trimmed};
        }
        String title = trimmed.substring(0, nl).trim();
        String body = trimmed.substring(nl + 1).trim();
        return new String[]{title, body};
    }
}
