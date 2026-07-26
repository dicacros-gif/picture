package com.dicacros.picture;

import org.json.JSONObject;
import org.json.JSONTokener;

final class ChatGptWebAutomation {

    static final String STATE_JS =
            "(function(){try{"
                    + "var composer=document.querySelector('#prompt-textarea')"
                    + "||document.querySelector('textarea[data-testid]')"
                    + "||document.querySelector('main textarea')"
                    + "||document.querySelector('main [contenteditable=\"true\"]');"
                    + "var messages=document.querySelectorAll('[data-message-author-role=\"assistant\"]');"
                    + "var last=messages.length?messages[messages.length-1]:null;"
                    + "var stop=document.querySelector('[data-testid=\"stop-button\"]')"
                    + "||document.querySelector('button[aria-label*=\"Stop\"]')"
                    + "||document.querySelector('button[aria-label*=\"중지\"]');"
                    + "var busy=!!(stop&&stop.getClientRects().length&&!stop.disabled);"
                    + "return JSON.stringify({ready:!!composer,busy:busy,"
                    + "count:messages.length,text:last?(last.innerText||last.textContent||'').trim():''});"
                    + "}catch(e){return JSON.stringify({ready:false,busy:false,count:0,text:'',"
                    + "error:String(e)});}})();";

    static final String SEND_JS =
            "(function(){try{"
                    + "var buttons=document.querySelectorAll('button');var send=null;"
                    + "send=document.querySelector('[data-testid=\"send-button\"]')"
                    + "||document.querySelector('button[aria-label*=\"Send\"]')"
                    + "||document.querySelector('button[aria-label*=\"보내기\"]');"
                    + "if(!send){for(var i=0;i<buttons.length;i++){"
                    + "var label=((buttons[i].getAttribute('aria-label')||'')+' '+"
                    + "(buttons[i].innerText||'')).trim().toLowerCase();"
                    + "if(label==='send'||label.indexOf('메시지 보내기')>=0){send=buttons[i];break;}}}"
                    + "if(send&&send.getClientRects().length&&!send.disabled){"
                    + "send.click();return JSON.stringify({ok:true});}"
                    + "return JSON.stringify({ok:false,error:'보내기 버튼을 찾지 못했습니다'});"
                    + "}catch(e){return JSON.stringify({ok:false,error:String(e)});}})();";

    private ChatGptWebAutomation() {
    }

    static String fillPromptJs(String prompt) {
        String quoted = JSONObject.quote(prompt == null ? "" : prompt);
        return "(function(){try{var value=" + quoted + ";"
                + "var el=document.querySelector('#prompt-textarea')"
                + "||document.querySelector('textarea[data-testid]')"
                + "||document.querySelector('main textarea')"
                + "||document.querySelector('main [contenteditable=\"true\"]');"
                + "if(!el)return JSON.stringify({ok:false,error:'입력창을 찾지 못했습니다'});"
                + "el.focus();"
                + "if(el.tagName==='TEXTAREA'||el.tagName==='INPUT'){"
                + "var proto=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
                + "var setter=Object.getOwnPropertyDescriptor(proto,'value').set;"
                + "setter.call(el,value);"
                + "el.dispatchEvent(new Event('input',{bubbles:true}));"
                + "}else{"
                + "var range=document.createRange();range.selectNodeContents(el);"
                + "var selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);"
                + "var inserted=false;try{inserted=document.execCommand('insertText',false,value);}"
                + "catch(ignore){}"
                + "if(!inserted){el.innerHTML='';var p=document.createElement('p');"
                + "p.textContent=value;el.appendChild(p);}"
                + "try{el.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:value}));}"
                + "catch(ignore){el.dispatchEvent(new Event('input',{bubbles:true}));}"
                + "}"
                + "el.dispatchEvent(new Event('change',{bubbles:true}));"
                + "return JSON.stringify({ok:true});"
                + "}catch(e){return JSON.stringify({ok:false,error:String(e)});}})();";
    }

    static State parseState(String rawValue) {
        JSONObject object = parseObject(rawValue);
        return new State(
                object.optBoolean("ready", false),
                object.optBoolean("busy", false),
                object.optInt("count", 0),
                object.optString("text", ""),
                object.optString("error", ""));
    }

    static ActionResult parseAction(String rawValue) {
        JSONObject object = parseObject(rawValue);
        return new ActionResult(
                object.optBoolean("ok", false),
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

    static final class State {
        final boolean ready;
        final boolean busy;
        final int assistantCount;
        final String text;
        final String error;

        State(boolean ready, boolean busy, int assistantCount, String text, String error) {
            this.ready = ready;
            this.busy = busy;
            this.assistantCount = assistantCount;
            this.text = text;
            this.error = error;
        }
    }

    static final class ActionResult {
        final boolean ok;
        final String error;

        ActionResult(boolean ok, String error) {
            this.ok = ok;
            this.error = error;
        }
    }
}
