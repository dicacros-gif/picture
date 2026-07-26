package com.dicacros.picture;

import android.webkit.WebView;

import org.json.JSONObject;
import org.json.JSONTokener;

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
                + "var docs=[document];var frames=document.querySelectorAll('iframe');"
                + "for(var f=0;f<frames.length;f++){try{if(frames[f].contentDocument)"
                + "docs.push(frames[f].contentDocument);}catch(ignore){}}"
                + "function visible(el){return !!(el&&el.getClientRects().length"
                + "&&el.offsetWidth>0&&el.offsetHeight>0);}"
                + "function fire(el){['beforeinput','input','keyup','change','blur'].forEach(function(ev){"
                + "el.dispatchEvent(new Event(ev,{bubbles:true}));});}"
                + "function setInput(el,v){if(!el)return false;"
                + "if(el.isContentEditable){el.focus();"
                + "var owner=el.ownerDocument;var selection=owner.defaultView.getSelection();"
                + "var range=owner.createRange();range.selectNodeContents(el);"
                + "selection.removeAllRanges();selection.addRange(range);"
                + "var inserted=false;try{inserted=owner.execCommand('insertText',false,v);}"
                + "catch(ignore){}if(!inserted){el.textContent=v;}fire(el);return true;}"
                + "if('value' in el){el.focus();"
                + "var win=el.ownerDocument.defaultView;"
                + "var proto=el.tagName==='TEXTAREA'?win.HTMLTextAreaElement.prototype:win.HTMLInputElement.prototype;"
                + "var setter=Object.getOwnPropertyDescriptor(proto,'value');"
                + "if(setter&&setter.set)setter.set.call(el,v);else el.value=v;"
                + "fire(el);return true;}return false;}"
                + "var tSel=['textarea[placeholder*=\"제목\"]','input[placeholder*=\"제목\"]',"
                + "'[contenteditable][data-placeholder*=\"제목\"]','.se-title-text',"
                + "'.se-title-text p','[contenteditable][class*=title]','.htitle'];"
                + "var okT=false;var titleEl=null;"
                + "for(var d=0;d<docs.length&&!okT;d++){for(var i=0;i<tSel.length&&!okT;i++){"
                + "titleEl=docs[d].querySelector(tSel[i]);"
                + "if(visible(titleEl))okT=setInput(titleEl,title);}}"
                + "var bodyEl=null;var bSel=["
                + "'[contenteditable][data-placeholder*=\"내용\"]',"
                + "'[contenteditable][aria-label*=\"본문\"]',"
                + "'.se-component.se-text .se-text-paragraph',"
                + "'.se-text-paragraph','.se-component-content',"
                + "'#editorArea','[contenteditable=\"true\"]','textarea','.editor'];"
                + "for(var s=0;s<bSel.length&&!bodyEl;s++){"
                + "for(var j=0;j<docs.length&&!bodyEl;j++){"
                + "var values=docs[j].querySelectorAll(bSel[s]);"
                + "for(var k=0;k<values.length;k++){var value=values[k];"
                + "if(value!==titleEl&&visible(value)&&value.offsetHeight>24){"
                + "bodyEl=value;break;}}}}"
                + "var okB=setInput(bodyEl,body);"
                + "return JSON.stringify({title:okT,body:okB,"
                + "bodyLength:okB?body.length:0});"
                + "}catch(e){return JSON.stringify({error:String(e)});}})();";
    }

    static final String EDITOR_STATE_JS =
            "(function(){try{"
                    + "var docs=[document];var frames=document.querySelectorAll('iframe');"
                    + "for(var f=0;f<frames.length;f++){try{if(frames[f].contentDocument)"
                    + "docs.push(frames[f].contentDocument);}catch(ignore){}}"
                    + "function visible(el){return !!(el&&el.getClientRects().length"
                    + "&&el.offsetWidth>0&&el.offsetHeight>0);}"
                    + "function first(selectors){for(var d=0;d<docs.length;d++){"
                    + "for(var i=0;i<selectors.length;i++){var el=docs[d].querySelector(selectors[i]);"
                    + "if(visible(el))return el;}}return null;}"
                    + "function firstExcept(selectors,excluded){for(var d=0;d<docs.length;d++){"
                    + "for(var i=0;i<selectors.length;i++){"
                    + "var values=docs[d].querySelectorAll(selectors[i]);"
                    + "for(var k=0;k<values.length;k++){"
                    + "if(values[k]!==excluded&&visible(values[k]))return values[k];}}}return null;}"
                    + "var title=first(['textarea[placeholder*=제목]','input[placeholder*=제목]',"
                    + "'[contenteditable][data-placeholder*=제목]','.se-title-text',"
                    + "'[contenteditable][class*=title]']);"
                    + "var body=firstExcept(['[contenteditable][data-placeholder*=내용]',"
                    + "'[contenteditable][aria-label*=본문]',"
                    + "'.se-component.se-text .se-text-paragraph','.se-text-paragraph',"
                    + "'.se-component-content','#editorArea','[contenteditable=true]'],title);"
                    + "var imageCount=0;var imageSelectors=["
                    + "'.se-component.se-image','.se-module-image img','.se-image-resource',"
                    + "'[data-component-name*=image]','img[src^=\"blob:\"]',"
                    + "'img[src*=\"postfiles\"]'];"
                    + "for(var d2=0;d2<docs.length;d2++){for(var s=0;s<imageSelectors.length;s++){"
                    + "imageCount+=docs[d2].querySelectorAll(imageSelectors[s]).length;}}"
                    + "var busy=!!first(['[aria-busy=true]','.se-loading','.loading',"
                    + "'[class*=progress][style*=display]']);"
                    + "var publishReady=false;"
                    + "for(var d3=0;d3<docs.length&&!publishReady;d3++){"
                    + "var controls=docs[d3].querySelectorAll('button,a,[role=button]');"
                    + "for(var c=0;c<controls.length;c++){if(!visible(controls[c]))continue;"
                    + "var label=((controls[c].getAttribute('aria-label')||'')+' '+"
                    + "(controls[c].innerText||controls[c].textContent||'')).trim();"
                    + "if(label==='발행'||label==='등록'||label==='게시'||label==='완료'"
                    + "||label.indexOf('발행하기')>=0){publishReady=true;break;}}}"
                    + "var text=((document.body&&document.body.innerText)||'');"
                    + "var success=text.indexOf('발행되었습니다')>=0"
                    + "||text.indexOf('게시글이 등록')>=0"
                    + "||text.indexOf('글이 등록')>=0;"
                    + "var loginRequired=!title&&!body"
                    + "&&(text.indexOf('로그인')>=0||location.href.indexOf('nidlogin')>=0);"
                    + "function valueLength(el){if(!el)return 0;"
                    + "var value=('value' in el)?el.value:(el.innerText||el.textContent||'');"
                    + "return (value||'').trim().length;}"
                    + "return JSON.stringify({titleReady:!!title,bodyReady:!!body,"
                    + "titleLength:valueLength(title),bodyLength:valueLength(body),"
                    + "editorReady:!!(title||body),imageCount:imageCount,busy:busy,"
                    + "publishReady:publishReady,success:success,"
                    + "loginRequired:loginRequired,url:location.href});"
                    + "}catch(e){return JSON.stringify({editorReady:false,error:String(e)});}})();";

    /** 발행/등록/확인 계열 버튼을 눌러 최종 발행까지 진행하는 JS. */
    static final String PUBLISH_JS =
            "(function(){try{"
                    + "var docs=[document];var frames=document.querySelectorAll('iframe');"
                    + "for(var f=0;f<frames.length;f++){try{if(frames[f].contentDocument)"
                    + "docs.push(frames[f].contentDocument);}catch(ignore){}}"
                    + "function visible(el){return !!(el&&el.getClientRects().length"
                    + "&&el.offsetWidth>0&&el.offsetHeight>0);}"
                    + "function clickByText(texts){"
                    + "for(var k=0;k<texts.length;k++){for(var d=0;d<docs.length;d++){"
                    + "var els=docs[d].querySelectorAll('button,a,[role=button]');"
                    + "for(var i=0;i<els.length;i++){"
                    + "var el=els[i];if(!visible(el)||el.disabled)continue;"
                    + "var t=((el.getAttribute('aria-label')||'')+' '+"
                    + "(el.innerText||el.textContent||'')).trim();"
                    + "if(t===texts[k]||t.indexOf(texts[k]+'하기')>=0){"
                    + "el.click();return t;}}}}return null;}"
                    + "var a=clickByText(['발행','등록','게시','확인','완료']);"
                    + "return JSON.stringify({clicked:a});}catch(e){return JSON.stringify({error:String(e)});}})();";

    static final String OPEN_IMAGE_PICKER_JS =
            "(function(){try{"
                    + "var docs=[document];var frames=document.querySelectorAll('iframe');"
                    + "for(var f=0;f<frames.length;f++){try{if(frames[f].contentDocument)"
                    + "docs.push(frames[f].contentDocument);}catch(ignore){}}"
                    + "function visible(el){return !!(el&&el.getClientRects().length"
                    + "&&el.offsetWidth>0&&el.offsetHeight>0);}"
                    + "var editor=null;var editorSelectors=["
                    + "'[contenteditable][data-placeholder*=\"내용\"]',"
                    + "'[contenteditable][aria-label*=\"본문\"]',"
                    + "'.se-component.se-text .se-text-paragraph',"
                    + "'.se-text-paragraph','#editorArea','[contenteditable=\"true\"]'];"
                    + "for(var s=0;s<editorSelectors.length&&!editor;s++){"
                    + "for(var d=0;d<docs.length&&!editor;d++){"
                    + "var values=docs[d].querySelectorAll(editorSelectors[s]);"
                    + "for(var v=0;v<values.length;v++){"
                    + "var label=((values[v].getAttribute('data-placeholder')||'')+' '+"
                    + "(values[v].getAttribute('aria-label')||'')).trim();"
                    + "if(visible(values[v])&&label.indexOf('제목')<0){"
                    + "editor=values[v];break;}}}}"
                    + "if(editor){editor.focus();var owner=editor.ownerDocument;"
                    + "var range=owner.createRange();"
                    + "var selection=owner.defaultView.getSelection();var children=editor.childNodes;"
                    + "var middle=Math.floor(children.length/2);"
                    + "if(children.length===1&&children[0].nodeType===3){"
                    + "var textNode=children[0];range.setStart(textNode,"
                    + "Math.floor((textNode.nodeValue||'').length/2));}"
                    + "else{range.setStart(editor,middle);}range.collapse(true);"
                    + "selection.removeAllRanges();selection.addRange(range);}"
                    + "for(var j=0;j<docs.length;j++){"
                    + "var buttons=docs[j].querySelectorAll('button,a,[role=button],label');"
                    + "for(var i=0;i<buttons.length;i++){var b=buttons[i];"
                    + "var text=((b.getAttribute('aria-label')||'')+' '+"
                    + "(b.getAttribute('title')||'')+' '+(b.innerText||'')).trim();"
                    + "if((text.indexOf('사진')>=0||text.indexOf('이미지')>=0)"
                    + "&&text.indexOf('삭제')<0&&b.offsetHeight>0){b.click();"
                    + "return JSON.stringify({clicked:text});}}}"
                    + "return JSON.stringify({clicked:null});"
                    + "}catch(e){return JSON.stringify({error:String(e)});}})();";

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

    static void runOpenImagePicker(WebView web, StepResult cb) {
        web.evaluateJavascript(OPEN_IMAGE_PICKER_JS, value -> {
            if (cb != null) {
                cb.onResult(value);
            }
        });
    }

    static void runState(WebView web, StepResult cb) {
        web.evaluateJavascript(EDITOR_STATE_JS, value -> {
            if (cb != null) {
                cb.onResult(value);
            }
        });
    }

    static Action parseAction(String rawValue) {
        JSONObject object = parseObject(rawValue);
        String clicked = object.isNull("clicked")
                ? "" : object.optString("clicked", "");
        if ("null".equalsIgnoreCase(clicked)) {
            clicked = "";
        }
        return new Action(
                object.optBoolean("title", false),
                object.optBoolean("body", false),
                object.optInt("bodyLength", 0),
                clicked,
                object.optString("error", ""));
    }

    static State parseState(String rawValue) {
        JSONObject object = parseObject(rawValue);
        return new State(
                object.optBoolean("titleReady", false),
                object.optBoolean("bodyReady", false),
                object.optBoolean("editorReady", false),
                object.optInt("titleLength", 0),
                object.optInt("bodyLength", 0),
                object.optInt("imageCount", 0),
                object.optBoolean("busy", false),
                object.optBoolean("publishReady", false),
                object.optBoolean("success", false),
                object.optBoolean("loginRequired", false),
                object.optString("url", ""),
                object.optString("error", ""));
    }

    private static JSONObject parseObject(String rawValue) {
        if (rawValue == null || rawValue.isEmpty() || "null".equals(rawValue)) {
            return new JSONObject();
        }
        try {
            Object value = new JSONTokener(rawValue).nextValue();
            if (value instanceof JSONObject) {
                return (JSONObject) value;
            }
            if (value instanceof String) {
                return new JSONObject((String) value);
            }
        } catch (Exception ignored) {
        }
        return new JSONObject();
    }

    static final class Action {
        final boolean titleFilled;
        final boolean bodyFilled;
        final int bodyLength;
        final String clicked;
        final String error;

        Action(boolean titleFilled, boolean bodyFilled, int bodyLength,
               String clicked, String error) {
            this.titleFilled = titleFilled;
            this.bodyFilled = bodyFilled;
            this.bodyLength = bodyLength;
            this.clicked = clicked;
            this.error = error;
        }
    }

    static final class State {
        final boolean titleReady;
        final boolean bodyReady;
        final boolean editorReady;
        final int titleLength;
        final int bodyLength;
        final int imageCount;
        final boolean busy;
        final boolean publishReady;
        final boolean success;
        final boolean loginRequired;
        final String url;
        final String error;

        State(boolean titleReady, boolean bodyReady, boolean editorReady,
              int titleLength, int bodyLength, int imageCount,
              boolean busy, boolean publishReady,
              boolean success, boolean loginRequired, String url, String error) {
            this.titleReady = titleReady;
            this.bodyReady = bodyReady;
            this.editorReady = editorReady;
            this.titleLength = titleLength;
            this.bodyLength = bodyLength;
            this.imageCount = imageCount;
            this.busy = busy;
            this.publishReady = publishReady;
            this.success = success;
            this.loginRequired = loginRequired;
            this.url = url;
            this.error = error;
        }
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
