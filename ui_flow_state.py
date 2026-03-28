from __future__ import annotations


def main_neutral_focus_candidates(*, visible_page: str, search_visible: bool, adaptive_split_enabled: bool, adaptive_real_detail_visible: bool) -> tuple[str, ...]:
    if search_visible:
        return ('search_button', 'home_button', 'add_button')
    if visible_page in {'settings_page', 'settings_assets_page'}:
        return ('back_button', 'home_button', 'search_button', 'add_button')
    if visible_page == 'overview_page':
        if adaptive_split_enabled and adaptive_real_detail_visible:
            return ('back_button', 'home_button', 'search_button', 'add_button')
        return ('home_button', 'search_button', 'add_button', 'back_button')
    return ('home_button', 'search_button', 'add_button', 'back_button')


def detail_neutral_focus_slot(page_name: str) -> tuple[str, ...]:
    current = str(page_name or 'main').strip()
    if current == 'main':
        return ('icon_button',)
    if current == 'icon':
        return ('first_icon_page_button', 'icon_button')
    if current == 'css_assets':
        return ('css_add_button', 'css_dropdown', 'icon_button')
    if current == 'javascript_assets':
        return ('javascript_add_button', 'javascript_dropdown', 'icon_button')
    return ('icon_button',)


def next_search_toggle_state(*, current_visible: bool, current_text: str) -> dict[str, object]:
    next_visible = not bool(current_visible)
    if next_visible:
        return {
            'search_visible': True,
            'show_back_header': True,
            'autofocus_search_entry': True,
            'clear_entry_text': False,
            'reset_search_text': False,
            'restore_header_actions': False,
        }
    return {
        'search_visible': False,
        'show_back_header': False,
        'autofocus_search_entry': False,
        'clear_entry_text': bool(current_text),
        'reset_search_text': True,
        'restore_header_actions': True,
    }
