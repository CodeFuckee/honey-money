"""侧栏折叠/展开 E2E：桌面端汉堡按钮切换图标窄条、状态持久化、移动端抽屉不受影响。

桌面端（>820px）：点标题栏汉堡 → 侧栏收窄为 56px 图标条（文字隐藏、图标仍可
导航），再点恢复展开；折叠状态写 localStorage（hm-sidebar-collapsed），刷新保持。
移动端（≤820px）：汉堡仍开合抽屉（.open），折叠类不改变移动端布局。
"""

import re

from playwright.sync_api import expect

MENU = ".shell-menu"
SIDEBAR = ".sidebar"


def nav_item(page, label):
    return page.locator(f"{SIDEBAR} .nav-item", has_text=label)


def sidebar_width(page) -> float:
    return page.locator(SIDEBAR).bounding_box()["width"]


def item_width(page, label) -> float:
    return nav_item(page, label).bounding_box()["width"]


class TestSidebarCollapse:
    def test_desktop_default_expanded(self, servers, page):
        """P0 桌面端默认展开：无 collapsed 类、宽度 ~256px、导航文字撑宽行。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        sidebar = page.locator(SIDEBAR)
        expect(sidebar).to_be_visible()
        expect(sidebar).not_to_have_class(re.compile("collapsed"))
        assert sidebar_width(page) > 200
        assert item_width(page, "仪表盘") > 100

    def test_hamburger_toggles_rail(self, servers, page):
        """P0 汉堡按钮切换：展开→窄条（行收窄只剩图标）→再点恢复展开。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        sidebar = page.locator(SIDEBAR)
        page.locator(MENU).click()
        expect(sidebar).to_have_class(re.compile("collapsed"))
        page.wait_for_timeout(300)                     # 宽度过渡 .2s
        assert sidebar_width(page) < 100
        assert item_width(page, "仪表盘") < 48         # 文字隐藏，只剩图标
        page.locator(MENU).click()
        expect(sidebar).not_to_have_class(re.compile("collapsed"))
        page.wait_for_timeout(300)
        assert sidebar_width(page) > 200
        assert item_width(page, "仪表盘") > 100

    def test_rail_still_navigates(self, servers, page):
        """P1 窄条模式下图标仍可导航：点「数据」图标切到数据页。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        page.locator(MENU).click()
        expect(page.locator(SIDEBAR)).to_have_class(re.compile("collapsed"))
        nav_item(page, "数据").click()
        expect(page.get_by_role("heading", name="数据浏览")).to_be_visible()

    def test_persistence_across_reload(self, servers, page):
        """P0 状态记忆：折叠→刷新仍窄条；展开→刷新仍展开（localStorage）。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        page.locator(MENU).click()
        expect(page.locator(SIDEBAR)).to_have_class(re.compile("collapsed"))
        page.reload()
        expect(page.locator(SIDEBAR)).to_have_class(re.compile("collapsed"))
        page.wait_for_timeout(300)
        assert sidebar_width(page) < 100
        page.locator(MENU).click()                     # 再展开
        page.reload()
        expect(page.locator(SIDEBAR)).not_to_have_class(re.compile("collapsed"))
        page.wait_for_timeout(300)
        assert sidebar_width(page) > 200

    def test_rapid_toggle_state_consistent(self, servers, page):
        """P1 边界：快速连点 3 次终态=窄条（奇数次），再点 1 次恢复展开。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        for _ in range(3):
            page.locator(MENU).click()
        expect(page.locator(SIDEBAR)).to_have_class(re.compile("collapsed"))
        page.locator(MENU).click()
        expect(page.locator(SIDEBAR)).not_to_have_class(re.compile("collapsed"))


class TestMobileDrawerUnaffected:
    def test_mobile_drawer_opens(self, servers, page):
        """P1 移动端（≤820px）：汉堡开合抽屉，导航文字可见可点。"""
        _, frontend_port = servers
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        sidebar = page.locator(SIDEBAR)
        expect(sidebar).not_to_have_class(re.compile("open"))
        page.locator(MENU).click()
        expect(sidebar).to_have_class(re.compile("open"))
        expect(page.get_by_role("button", name="仪表盘")).to_be_visible()
        page.get_by_role("button", name="仪表盘").click()   # 选中即收起
        expect(sidebar).not_to_have_class(re.compile("open"))

    def test_desktop_collapse_state_not_break_mobile(self, servers, page):
        """P1 桌面折叠态残留到移动端：布局仍是横排文字抽屉，文字不被隐藏。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        page.locator(MENU).click()
        expect(page.locator(SIDEBAR)).to_have_class(re.compile("collapsed"))
        page.set_viewport_size({"width": 390, "height": 844})
        # 抽屉收起（未点汉堡）；导航文字不因 collapsed 被隐藏，开合仍正常
        expect(page.get_by_role("button", name="仪表盘")).to_be_visible()
        page.locator(MENU).click()
        expect(page.locator(SIDEBAR)).to_have_class(re.compile("open"))


class TestContentSidePadding:
    def test_content_side_padding_narrowed(self, servers, page):
        """P1 内容区左右边距收窄（用户反馈 40px 太大）：--content-pad
        左右 20px，折叠/展开两态一致。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        pad = page.evaluate("""() => {
          const cs = getComputedStyle(document.querySelector('.content'));
          return [cs.paddingLeft, cs.paddingRight];
        }""")
        assert pad == ["20px", "20px"], pad
        page.locator(MENU).click()                     # 折叠后同样 20px
        expect(page.locator(SIDEBAR)).to_have_class(re.compile("collapsed"))
        pad2 = page.evaluate("""() => {
          const cs = getComputedStyle(document.querySelector('.content'));
          return [cs.paddingLeft, cs.paddingRight];
        }""")
        assert pad2 == ["20px", "20px"], pad2
