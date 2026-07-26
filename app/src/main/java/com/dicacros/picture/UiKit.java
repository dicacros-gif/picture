package com.dicacros.picture;

import android.app.Activity;
import android.content.Context;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.ColorDrawable;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.StateListDrawable;
import android.os.Build;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

final class UiKit {

    static final int BACKGROUND = Color.rgb(244, 247, 251);
    static final int SURFACE = Color.WHITE;
    static final int SURFACE_SOFT = Color.rgb(248, 250, 252);
    static final int INK = Color.rgb(15, 23, 42);
    static final int MUTED = Color.rgb(100, 116, 139);
    static final int BORDER = Color.rgb(226, 232, 240);
    static final int PRIMARY = Color.rgb(37, 99, 235);
    static final int PRIMARY_DARK = Color.rgb(29, 78, 216);
    static final int TEAL = Color.rgb(13, 148, 136);
    static final int NAVER = Color.rgb(3, 158, 72);
    static final int NAVY = Color.rgb(30, 64, 175);
    static final int SUCCESS = Color.rgb(22, 101, 52);
    static final int SUCCESS_SOFT = Color.rgb(240, 253, 244);
    static final int INFO_SOFT = Color.rgb(239, 246, 255);
    static final int WARNING_SOFT = Color.rgb(255, 251, 235);

    private UiKit() {
    }

    static LinearLayout screen(Context context) {
        LinearLayout root = new LinearLayout(context);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(context, 18), dp(context, 22), dp(context, 18), dp(context, 30));
        root.setBackgroundColor(BACKGROUND);
        return root;
    }

    static LinearLayout card(Context context) {
        LinearLayout card = new LinearLayout(context);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(context, 16), dp(context, 16), dp(context, 16), dp(context, 16));
        card.setBackground(rounded(SURFACE, 18, context));
        if (Build.VERSION.SDK_INT >= 21) {
            card.setElevation(dp(context, 2));
        }
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.setMargins(0, 0, 0, dp(context, 12));
        card.setLayoutParams(params);
        return card;
    }

    static LinearLayout backBar(Activity activity, String section) {
        LinearLayout bar = new LinearLayout(activity);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        LinearLayout.LayoutParams barParams = new LinearLayout.LayoutParams(-1, dp(activity, 48));
        barParams.setMargins(0, 0, 0, dp(activity, 10));
        bar.setLayoutParams(barParams);

        Button back = secondaryButton(activity, "←");
        back.setContentDescription("뒤로 가기");
        back.setTextSize(20);
        back.setOnClickListener(view -> activity.finish());
        LinearLayout.LayoutParams backParams =
                new LinearLayout.LayoutParams(dp(activity, 48), dp(activity, 48));
        back.setLayoutParams(backParams);
        bar.addView(back);

        TextView label = text(activity, section, 14, MUTED, true);
        LinearLayout.LayoutParams labelParams =
                new LinearLayout.LayoutParams(0, -2, 1f);
        labelParams.setMargins(dp(activity, 12), 0, 0, 0);
        bar.addView(label, labelParams);
        return bar;
    }

    static LinearLayout tintedCard(Context context, int fill, int stroke) {
        LinearLayout card = card(context);
        card.setBackground(stroked(fill, stroke, 18, 1, context));
        if (Build.VERSION.SDK_INT >= 21) {
            card.setElevation(0);
        }
        return card;
    }

    static TextView eyebrow(Context context, String text) {
        TextView view = text(context, text, 12, PRIMARY, true);
        view.setLetterSpacing(0.08f);
        view.setPadding(0, 0, 0, dp(context, 7));
        return view;
    }

    static TextView pageTitle(Context context, String text) {
        TextView view = text(context, text, 29, INK, true);
        view.setLineSpacing(0, 1.04f);
        view.setPadding(0, 0, 0, dp(context, 8));
        return view;
    }

    static TextView sectionTitle(Context context, String text) {
        TextView view = text(context, text, 17, INK, true);
        view.setPadding(0, 0, 0, dp(context, 8));
        return view;
    }

    static TextView body(Context context, String text) {
        TextView view = text(context, text, 14, MUTED, false);
        view.setLineSpacing(dp(context, 3), 1f);
        return view;
    }

    static TextView caption(Context context, String text) {
        TextView view = text(context, text, 12, MUTED, false);
        view.setLineSpacing(dp(context, 2), 1f);
        return view;
    }

    static TextView badge(Context context, String text, int color) {
        TextView badge = text(context, text, 11, color, true);
        badge.setGravity(Gravity.CENTER);
        badge.setPadding(dp(context, 10), dp(context, 5),
                dp(context, 10), dp(context, 5));
        badge.setBackground(stroked(withAlpha(color, 18), withAlpha(color, 80),
                999, 1, context));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-2, -2);
        params.setMargins(0, 0, 0, dp(context, 10));
        badge.setLayoutParams(params);
        return badge;
    }

    static TextView status(Context context) {
        TextView view = body(context, "");
        view.setTextColor(Color.rgb(30, 64, 175));
        view.setPadding(dp(context, 13), dp(context, 11),
                dp(context, 13), dp(context, 11));
        view.setBackground(stroked(INFO_SOFT, Color.rgb(191, 219, 254),
                12, 1, context));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.setMargins(0, dp(context, 10), 0, 0);
        view.setLayoutParams(params);
        return view;
    }

    static Button primaryButton(Context context, String text, int color) {
        Button button = baseButton(context, text);
        button.setTextColor(Color.WHITE);
        button.setBackground(pressed(color, darken(color, 0.84f), context));
        return button;
    }

    static Button secondaryButton(Context context, String text) {
        Button button = baseButton(context, text);
        button.setTextColor(INK);
        button.setBackground(pressedSurface(context));
        return button;
    }

    static void stylePrimary(Button button, int color) {
        button.setTextColor(Color.WHITE);
        button.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        button.setBackground(pressed(color, darken(color, 0.84f), button.getContext()));
    }

    static void styleSecondary(Button button) {
        button.setTextColor(INK);
        button.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        button.setBackground(pressedSurface(button.getContext()));
    }

    static void styleInput(EditText input, boolean multiline) {
        Context context = input.getContext();
        input.setTextColor(INK);
        input.setHintTextColor(Color.rgb(148, 163, 184));
        input.setTextSize(14);
        input.setPadding(dp(context, 13), multiline ? dp(context, 12) : 0,
                dp(context, 13), multiline ? dp(context, 12) : 0);
        input.setBackground(stroked(SURFACE, BORDER, 12, 1, context));
    }

    static void styleCheck(CheckBox box) {
        box.setTextColor(INK);
        box.setTextSize(14);
        box.setGravity(Gravity.CENTER_VERTICAL);
        box.setPadding(dp(box.getContext(), 2), dp(box.getContext(), 6),
                dp(box.getContext(), 2), dp(box.getContext(), 6));
        if (Build.VERSION.SDK_INT >= 21) {
            box.setButtonTintList(new ColorStateList(
                    new int[][]{
                            new int[]{android.R.attr.state_checked},
                            new int[]{}
                    },
                    new int[]{PRIMARY, Color.rgb(148, 163, 184)}));
        }
    }

    static void tintProgress(ProgressBar progressBar, int color) {
        if (Build.VERSION.SDK_INT >= 21) {
            progressBar.setProgressTintList(ColorStateList.valueOf(color));
            progressBar.setProgressBackgroundTintList(
                    ColorStateList.valueOf(Color.rgb(219, 234, 254)));
        }
    }

    static Drawable rounded(int color, int radiusDp, Context context) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(context, radiusDp));
        return drawable;
    }

    static Drawable stroked(int fill, int stroke, int radiusDp,
                            int strokeDp, Context context) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(fill);
        drawable.setCornerRadius(dp(context, radiusDp));
        drawable.setStroke(dp(context, strokeDp), stroke);
        return drawable;
    }

    static Drawable pressed(int normal, int pressed, Context context) {
        StateListDrawable states = new StateListDrawable();
        states.addState(new int[]{android.R.attr.state_pressed},
                rounded(pressed, 12, context));
        states.addState(new int[]{-android.R.attr.state_enabled},
                rounded(Color.rgb(203, 213, 225), 12, context));
        states.addState(new int[0], rounded(normal, 12, context));
        return states;
    }

    private static Drawable pressedSurface(Context context) {
        StateListDrawable states = new StateListDrawable();
        states.addState(new int[]{android.R.attr.state_pressed},
                stroked(Color.rgb(241, 245, 249), Color.rgb(203, 213, 225),
                        12, 1, context));
        states.addState(new int[]{-android.R.attr.state_enabled},
                new ColorDrawable(Color.rgb(241, 245, 249)));
        states.addState(new int[0],
                stroked(SURFACE, BORDER, 12, 1, context));
        return states;
    }

    private static Button baseButton(Context context, String text) {
        Button button = new Button(context);
        button.setAllCaps(false);
        button.setText(text);
        button.setTextSize(13);
        button.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        button.setGravity(Gravity.CENTER);
        button.setMinHeight(0);
        button.setMinimumHeight(0);
        button.setPadding(dp(context, 10), 0, dp(context, 10), 0);
        if (Build.VERSION.SDK_INT >= 21) {
            button.setStateListAnimator(null);
        }
        return button;
    }

    private static TextView text(Context context, String value, int size,
                                 int color, boolean medium) {
        TextView view = new TextView(context);
        view.setText(value);
        view.setTextSize(size);
        view.setTextColor(color);
        view.setTypeface(Typeface.create(
                medium ? "sans-serif-medium" : "sans-serif", Typeface.NORMAL));
        return view;
    }

    private static int withAlpha(int color, int alpha) {
        return Color.argb(alpha, Color.red(color), Color.green(color), Color.blue(color));
    }

    private static int darken(int color, float factor) {
        return Color.rgb(
                (int) (Color.red(color) * factor),
                (int) (Color.green(color) * factor),
                (int) (Color.blue(color) * factor));
    }

    static int dp(Context context, int value) {
        return (int) (value * context.getResources()
                .getDisplayMetrics().density + 0.5f);
    }
}
