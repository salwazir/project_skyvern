import asyncio
import copy
import json
import os
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, List

import pyotp
import structlog
from playwright.async_api import FileChooser, Frame, Locator, Page, TimeoutError
from pydantic import BaseModel

from skyvern.config import settings
from skyvern.constants import (
    AUTO_COMPLETION_POTENTIAL_VALUES_COUNT,
    BROWSER_DOWNLOAD_MAX_WAIT_TIME,
    DROPDOWN_MENU_MAX_DISTANCE,
    REPO_ROOT_DIR,
    SKYVERN_ID_ATTR,
)
from skyvern.exceptions import (
    DownloadFileMaxWaitingTime,
    EmptySelect,
    ErrEmptyTweakValue,
    ErrFoundSelectableElement,
    FailedToFetchSecret,
    FailToClick,
    FailToSelectByIndex,
    FailToSelectByValue,
    IllegitComplete,
    ImaginaryFileUrl,
    InputToInvisibleElement,
    InteractWithDisabledElement,
    InteractWithDropdownContainer,
    InvalidElementForTextInput,
    MissingElement,
    MissingElementDict,
    MissingElementInCSSMap,
    MissingFileUrl,
    MultipleElementsFound,
    NoAutoCompleteOptionMeetCondition,
    NoAvailableOptionFoundForCustomSelection,
    NoElementMatchedForTargetOption,
    NoIncrementalElementFoundForAutoCompletion,
    NoIncrementalElementFoundForCustomSelection,
    NoSuitableAutoCompleteOption,
    NoTOTPVerificationCodeFound,
    OptionIndexOutOfBound,
    WrongElementToUploadFile,
)
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import (
    download_file,
    get_download_dir,
    list_downloading_files_in_directory,
    list_files_in_directory,
    wait_for_download_finished,
)
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory, LLMCallerManager
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_post
from skyvern.forge.sdk.core.security import generate_skyvern_signature
from skyvern.forge.sdk.core.skyvern_context import ensure_context
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants
from skyvern.forge.sdk.services.credentials import OnePasswordConstants
from skyvern.forge.sdk.trace import TraceManager
from skyvern.services.task_v1_service import is_cua_task
from skyvern.utils.prompt_engine import CheckPhoneNumberFormatResponse, load_prompt_with_elements
from skyvern.webeye.actions import actions, handler_utils
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import (
    Action,
    ActionStatus,
    CheckboxAction,
    ClickAction,
    InputOrSelectContext,
    InputTextAction,
    ScrapeResult,
    SelectOption,
    SelectOptionAction,
    UploadFileAction,
    WebAction,
)
from skyvern.webeye.actions.responses import ActionAbort, ActionFailure, ActionResult, ActionSuccess
from skyvern.webeye.scraper.scraper import (
    CleanupElementTreeFunc,
    ElementTreeBuilder,
    IncrementalScrapePage,
    ScrapedPage,
    hash_element,
    json_to_html,
    trim_element_tree,
)
from skyvern.webeye.utils.dom import COMMON_INPUT_TAGS, DomUtil, InteractiveElement, SkyvernElement
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()


class CustomSingleSelectResult:
    def __init__(self, skyvern_frame: SkyvernFrame) -> None:
        self.reasoning: str | None = None
        self.action_result: ActionResult | None = None
        self.action_type: ActionType | None = None
        self.value: str | None = None
        self.dropdown_menu: SkyvernElement | None = None
        self.skyvern_frame = skyvern_frame

    async def is_done(self) -> bool:
        # check if the dropdown menu is still on the page
        # if it still exists, might mean there might be multi-level selection
        # FIXME: only able to execute multi-level selection logic when dropdown menu detected
        if self.dropdown_menu is None:
            return True

        if not isinstance(self.action_result, ActionSuccess):
            return True

        if await self.dropdown_menu.get_locator().count() == 0:
            return True

        return not await self.skyvern_frame.get_element_visible(await self.dropdown_menu.get_element_handler())


def is_ul_or_listbox_element_factory(
    incremental_scraped: IncrementalScrapePage, task: Task, step: Step
) -> Callable[[dict], Awaitable[bool]]:
    async def wrapper(element_dict: dict) -> bool:
        element_id: str = element_dict.get("id", "")
        try:
            element = await SkyvernElement.create_from_incremental(incremental_scraped, element_id)
        except Exception:
            LOG.debug(
                "Failed to element in the incremental page",
                element_id=element_id,
                step_id=step.step_id,
                task_id=task.task_id,
                exc_info=True,
            )
            return False

        if element.get_tag_name() == "ul":
            return True

        if await element.get_attr("role") == "listbox":
            return True

        return False

    return wrapper


CheckFilterOutElementIDFunc = Callable[[dict, Page | Frame], Awaitable[bool]]


def check_existed_but_not_option_element_in_dom_factory(
    dom: DomUtil,
) -> CheckFilterOutElementIDFunc:
    async def helper(element_dict: dict, frame: Page | Frame) -> bool:
        element_id: str = element_dict.get("id", "")
        if not element_id:
            return False
        try:
            locator = frame.locator(f"[{SKYVERN_ID_ATTR}={element_id}]")
            current_element = SkyvernElement(locator=locator, frame=frame, static_element=element_dict)
            if await current_element.is_custom_option():
                return False
            return await dom.check_id_in_dom(element_id)
        except Exception:
            LOG.debug(
                "Failed to check if the element is a custom option, going to keep the element in the incremental tree",
                exc_info=True,
                element_id=element_id,
            )
            return False

    return helper


def check_disappeared_element_id_in_incremental_factory(
    incremental_scraped: IncrementalScrapePage,
) -> CheckFilterOutElementIDFunc:
    current_element_to_dict = copy.deepcopy(incremental_scraped.id_to_css_dict)

    async def helper(element_dict: dict, frame: Page | Frame) -> bool:
        element_id: str = element_dict.get("id", "")
        if not current_element_to_dict.get(element_id, ""):
            return False

        try:
            skyvern_element = await SkyvernElement.create_from_incremental(
                incre_page=incremental_scraped, element_id=element_id
            )
        except Exception:
            LOG.debug(
                "Failed to create skyvern element, going to drop the element from incremental tree",
                exc_info=True,
                element_id=element_id,
            )
            return True

        skyvern_frame = incremental_scraped.skyvern_frame
        return not await skyvern_frame.get_element_visible(await skyvern_element.get_element_handler())

    return helper


async def filter_out_elements(
    frame: Page | Frame, element_tree: list[dict], check_filter: CheckFilterOutElementIDFunc
) -> list[dict]:
    new_element_tree = []
    for element in element_tree:
        children_elements = element.get("children", [])
        if len(children_elements) > 0:
            children_elements = await filter_out_elements(
                frame=frame, element_tree=children_elements, check_filter=check_filter
            )
        if await check_filter(element, frame):
            new_element_tree.extend(children_elements)
        else:
            element["children"] = children_elements
            new_element_tree.append(element)
    return new_element_tree


def clean_and_remove_element_tree_factory(
    task: Task, step: Step, check_filter_funcs: list[CheckFilterOutElementIDFunc]
) -> CleanupElementTreeFunc:
    async def helper_func(frame: Page | Frame, url: str, element_tree: list[dict]) -> list[dict]:
        element_tree = await app.AGENT_FUNCTION.cleanup_element_tree_factory(task=task, step=step)(
            frame, url, element_tree
        )
        for check_filter in check_filter_funcs:
            element_tree = await filter_out_elements(frame=frame, element_tree=element_tree, check_filter=check_filter)

        return element_tree

    return helper_func


async def check_phone_number_format(
    value: str,
    action: actions.InputTextAction,
    skyvern_element: SkyvernElement,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> str:
    # check the phone number format
    LOG.info(
        "Input is a tel input, trigger phone number format checking",
        action=action,
        element_id=skyvern_element.get_id(),
    )

    new_scraped_page = await scraped_page.generate_scraped_page_without_screenshots()
    html = new_scraped_page.build_element_tree(html_need_skyvern_attrs=False)
    prompt = prompt_engine.load_prompt(
        template="check-phone-number-format",
        context=action.intention,
        current_value=value,
        navigation_goal=task.navigation_goal,
        navigation_payload_str=json.dumps(task.navigation_payload),
        elements=html,
        local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
    )

    json_response = await app.SECONDARY_LLM_API_HANDLER(
        prompt=prompt, step=step, prompt_name="check-phone-number-format"
    )

    check_phone_number_format_response = CheckPhoneNumberFormatResponse.model_validate(json_response)
    if (
        not check_phone_number_format_response.is_phone_number_input
        or check_phone_number_format_response.is_current_format_correct
        or not check_phone_number_format_response.recommended_phone_number
    ):
        return value

    LOG.info(
        "The current phone number format is incorrect, using the recommended phone number",
        action=action,
        element_id=skyvern_element.get_id(),
        recommended_phone_number=check_phone_number_format_response.recommended_phone_number,
    )
    return check_phone_number_format_response.recommended_phone_number


class AutoCompletionResult(BaseModel):
    auto_completion_attempt: bool = False
    incremental_elements: list[dict] = []
    action_result: ActionResult = ActionSuccess()


class ActionHandler:
    _handled_action_types: dict[
        ActionType,
        Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ] = {}

    _setup_action_types: dict[
        ActionType,
        Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ] = {}

    _teardown_action_types: dict[
        ActionType,
        Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ] = {}

    @classmethod
    def register_action_type(
        cls,
        action_type: ActionType,
        handler: Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ) -> None:
        cls._handled_action_types[action_type] = handler

    @classmethod
    def register_setup_for_action_type(
        cls,
        action_type: ActionType,
        handler: Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ) -> None:
        cls._setup_action_types[action_type] = handler

    @classmethod
    def register_teardown_for_action_type(
        cls,
        action_type: ActionType,
        handler: Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ) -> None:
        cls._teardown_action_types[action_type] = handler

    @staticmethod
    @TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
    async def handle_action(
        scraped_page: ScrapedPage,
        task: Task,
        step: Step,
        page: Page,
        action: Action,
    ) -> list[ActionResult]:
        LOG.info("Handling action", action=action)
        actions_result: list[ActionResult] = []
        try:
            if action.action_type in ActionHandler._handled_action_types:
                invalid_web_action_check = check_for_invalid_web_action(action, page, scraped_page, task, step)
                if invalid_web_action_check:
                    actions_result.extend(invalid_web_action_check)
                    return actions_result

                # do setup before action handler
                if setup := ActionHandler._setup_action_types.get(action.action_type):
                    results = await setup(action, page, scraped_page, task, step)
                    actions_result.extend(results)
                    if results and results[-1] != ActionSuccess:
                        return actions_result

                # do the handler
                handler = ActionHandler._handled_action_types[action.action_type]
                results = await handler(action, page, scraped_page, task, step)
                actions_result.extend(results)
                llm_caller = LLMCallerManager.get_llm_caller(task.task_id)
                if not results or not isinstance(actions_result[-1], ActionSuccess):
                    if llm_caller and action.tool_call_id:
                        # add failure message to the llm caller
                        tool_call_result = {
                            "type": "tool_result",
                            "tool_use_id": action.tool_call_id,
                            "content": "Tool execution failed",
                        }
                        llm_caller.add_tool_result(tool_call_result)
                        LOG.info("Tool call result", tool_call_result=tool_call_result, action=action)
                    return actions_result

                if llm_caller and action.tool_call_id:
                    tool_call_result = {
                        "type": "tool_result",
                        "tool_use_id": action.tool_call_id,
                        "content": "Tool executed successfully",
                    }
                    LOG.info("Tool call result", tool_call_result=tool_call_result, action=action)
                    llm_caller.add_tool_result(tool_call_result)

                # do the teardown
                teardown = ActionHandler._teardown_action_types.get(action.action_type)
                if teardown:
                    results = await teardown(action, page, scraped_page, task, step)
                    actions_result.extend(results)

                return actions_result

            else:
                LOG.error(
                    "Unsupported action type in handler",
                    action=action,
                    type=type(action),
                )
                actions_result.append(ActionFailure(Exception(f"Unsupported action type: {type(action)}")))
                return actions_result
        except MissingElement as e:
            LOG.info(
                "Known exceptions",
                action=action,
                exception_type=type(e),
                exception_message=str(e),
            )
            actions_result.append(ActionFailure(e))
        except MultipleElementsFound as e:
            LOG.exception(
                "Cannot handle multiple elements with the same selector in one action.",
                action=action,
            )
            actions_result.append(ActionFailure(e))
        except LLMProviderError as e:
            LOG.exception("LLM error in action handler", action=action, exc_info=True)
            actions_result.append(ActionFailure(e))
        except Exception as e:
            LOG.exception("Unhandled exception in action handler", action=action)
            actions_result.append(ActionFailure(e))
        finally:
            if actions_result and isinstance(actions_result[-1], ActionSuccess):
                action.status = ActionStatus.completed
            elif actions_result and isinstance(actions_result[-1], ActionAbort):
                action.status = ActionStatus.skipped
            else:
                # either actions_result is empty or the last action is a failure
                if not actions_result:
                    LOG.warning("Action failed to execute, setting status to failed", action=action)
                action.status = ActionStatus.failed
            await app.DATABASE.create_action(action=action)

        return actions_result


def check_for_invalid_web_action(
    action: actions.Action,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if isinstance(action, ClickAction) and action.x is not None and action.y is not None:
        return []

    if isinstance(action, InputTextAction) and not action.element_id:
        return []

    if isinstance(action, WebAction) and action.element_id not in scraped_page.id_to_element_dict:
        return [ActionFailure(MissingElement(element_id=action.element_id), stop_execution_on_failure=False)]

    return []


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_solve_captcha_action(
    action: actions.SolveCaptchaAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    LOG.warning(
        "Please solve the captcha on the page, you have 30 seconds",
        action=action,
    )
    await asyncio.sleep(30)
    return [ActionSuccess()]


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_click_action(
    action: actions.ClickAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    dom = DomUtil(scraped_page=scraped_page, page=page)
    original_url = page.url
    if action.x is not None and action.y is not None:
        # Find the element at the clicked location using JavaScript evaluation
        element_id: str | None = await page.evaluate(
            """data => {
            const element = document.elementFromPoint(data.x, data.y);
            if (!element) return null;

            // Function to get the unique_id attribute of an element
            function getElementUniqueId(element) {
                if (element && element.nodeType === 1) {
                    // Check if the element has the unique_id attribute
                    if (element.hasAttribute('unique_id')) {
                        return element.getAttribute('unique_id');
                    }
                    
                    // If no unique_id attribute is found, return null
                    return null;
                }
                return null;
            }

            return getElementUniqueId(element);
        }""",
            {"x": action.x, "y": action.y},
        )
        LOG.info("Clicked element at location", x=action.x, y=action.y, element_id=element_id, button=action.button)
        if element_id:
            skyvern_element = await dom.get_skyvern_element_by_id(element_id)
            if await skyvern_element.navigate_to_a_href(page=page):
                return [ActionSuccess()]

        if action.repeat == 1:
            await page.mouse.click(x=action.x, y=action.y, button=action.button)
        elif action.repeat == 2:
            await page.mouse.dblclick(x=action.x, y=action.y, button=action.button)
        elif action.repeat == 3:
            await page.mouse.click(x=action.x, y=action.y, button=action.button, click_count=3)
        else:
            raise ValueError(f"Invalid repeat value: {action.repeat}")

        return [ActionSuccess()]

    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    await asyncio.sleep(0.3)

    # dynamically validate the attr, since it could change into enabled after the previous actions
    if await skyvern_element.is_disabled(dynamic=True):
        LOG.warning(
            "Try to click on a disabled element",
            action_type=action.action_type,
            task_id=task.task_id,
            step_id=step.step_id,
            element_id=skyvern_element.get_id(),
        )
        return [ActionFailure(InteractWithDisabledElement(skyvern_element.get_id()))]

    if action.download:
        # get the initial page count
        browser_state = app.BROWSER_MANAGER.get_for_task(task.task_id, workflow_run_id=task.workflow_run_id)
        initial_page_count = 0
        if browser_state is not None:
            initial_page_count = len(browser_state.browser_context.pages if browser_state.browser_context else [])
        LOG.info(
            "Page count before download file action",
            initial_page_count=initial_page_count,
            task_id=task.task_id,
            step_id=step.step_id,
            workflow_run_id=task.workflow_run_id,
        )
        results: list[ActionResult] = []
        try:
            results = await handle_click_to_download_file_action(action, page, scraped_page, task, step)
        except Exception:
            raise
        finally:
            # get the page count after download
            page_count_after_download = 0
            if browser_state is not None:
                page_count_after_download = len(
                    browser_state.browser_context.pages if browser_state.browser_context else []
                )

            LOG.info(
                "Page count after download file action",
                initial_page_count=initial_page_count,
                page_count_after_download=page_count_after_download,
                task_id=task.task_id,
                step_id=step.step_id,
                workflow_run_id=task.workflow_run_id,
            )
            if page_count_after_download > initial_page_count and browser_state and browser_state.browser_context:
                if results and results[-1].download_triggered:
                    LOG.info(
                        "Download triggered, closing the extra page",
                        task_id=task.task_id,
                        step_id=step.step_id,
                        workflow_run_id=task.workflow_run_id,
                    )

                    if page == browser_state.browser_context.pages[-1]:
                        LOG.warning(
                            "The extra page is the current page, closing it",
                            task_id=task.task_id,
                            step_id=step.step_id,
                            workflow_run_id=task.workflow_run_id,
                        )
                    # close the extra page
                    await browser_state.browser_context.pages[-1].close()
                else:
                    LOG.info(
                        "No download triggered, not closing the extra page",
                        task_id=task.task_id,
                        step_id=step.step_id,
                        workflow_run_id=task.workflow_run_id,
                    )
    else:
        incremental_scraped: IncrementalScrapePage | None = None
        try:
            skyvern_frame = await SkyvernFrame.create_instance(skyvern_element.get_frame())
            incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame)
            await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())

            results = await chain_click(
                task,
                scraped_page,
                page,
                action,
                skyvern_element,
                timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
            )
            if page.url != original_url:
                return results

            if results and not isinstance(results[-1], ActionSuccess):
                return results

            try:
                if sequential_click_result := await handle_sequential_click_for_dropdown(
                    action=action,
                    anchor_element=skyvern_element,
                    dom=dom,
                    page=page,
                    scraped_page=scraped_page,
                    incremental_scraped=incremental_scraped,
                    task=task,
                    step=step,
                ):
                    results.append(sequential_click_result)
                    return results

            except Exception:
                LOG.warning(
                    "Failed to do sequential logic for the click action, skipping",
                    exc_info=True,
                    step_id=step.step_id,
                    task_id=task.task_id,
                    element_id=skyvern_element.get_id(),
                )
                return results

        finally:
            if incremental_scraped:
                await incremental_scraped.stop_listen_dom_increment()

    return results


@TraceManager.traced_async(ignore_inputs=["anchor_element", "scraped_page", "page", "incremental_scraped", "dom"])
async def handle_sequential_click_for_dropdown(
    action: actions.ClickAction,
    anchor_element: SkyvernElement,
    dom: DomUtil,
    page: Page,
    scraped_page: ScrapedPage,
    incremental_scraped: IncrementalScrapePage,
    task: Task,
    step: Step,
) -> ActionResult | None:
    if await incremental_scraped.get_incremental_elements_num() == 0:
        return None

    incremental_elements = await incremental_scraped.get_incremental_element_tree(
        clean_and_remove_element_tree_factory(
            task=task, step=step, check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)]
        ),
    )

    if len(incremental_elements) == 0:
        return None

    LOG.info("Detected new element after clicking", action=action)
    dropdown_menu_element = await locate_dropdown_menu(
        current_anchor_element=anchor_element,
        incremental_scraped=incremental_scraped,
        step=step,
        task=task,
    )

    if dropdown_menu_element is None:
        return None

    dropdown_select_context = await _get_input_or_select_context(
        action=AbstractActionForContextParse(
            reasoning=action.reasoning, intention=action.intention, element_id=action.element_id
        ),
        step=step,
        scraped_page=scraped_page,
    )

    if dropdown_select_context.is_date_related:
        LOG.info(
            "The dropdown is date related, exiting the sequential click logic",
            step_id=step.step_id,
            task_id=task.task_id,
        )
        return None

    LOG.info(
        "Found the dropdown menu element after clicking, triggering the sequential click logic",
        step_id=step.step_id,
        task_id=task.task_id,
        element_id=dropdown_menu_element.get_id(),
    )

    return await select_from_emerging_elements(
        current_element_id=anchor_element.get_id(),
        options=CustomSelectPromptOptions(
            field_information=dropdown_select_context.intention
            if dropdown_select_context.intention
            else dropdown_select_context.field,
            is_date_related=dropdown_select_context.is_date_related,
            required_field=dropdown_select_context.is_required,
        ),
        page=page,
        scraped_page=scraped_page,
        step=step,
        task=task,
        support_complete_action=True,
    )


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_click_to_download_file_action(
    action: actions.ClickAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    locator = skyvern_element.locator

    download_dir = Path(get_download_dir(workflow_run_id=task.workflow_run_id, task_id=task.task_id))
    list_files_before = list_files_in_directory(download_dir)
    LOG.info(
        "Number of files in download directory before click",
        num_downloaded_files_before=len(list_files_before),
        download_dir=download_dir,
        task_id=task.task_id,
        step_id=step.step_id,
        workflow_run_id=task.workflow_run_id,
    )

    try:
        if not await skyvern_element.navigate_to_a_href(page=page):
            await locator.click(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
        await page.wait_for_load_state(timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
    except Exception as e:
        LOG.exception(
            "ClickAction with download failed",
            exc_info=True,
            action=action,
            task_id=task.task_id,
            step_id=step.step_id,
            workflow_run_id=task.workflow_run_id,
        )
        return [ActionFailure(e, download_triggered=False)]

    try:
        LOG.info(
            "Checking if there is any new files after click",
            download_dir=download_dir,
        )
        async with asyncio.timeout(BROWSER_DOWNLOAD_MAX_WAIT_TIME):
            while True:
                list_files_after = list_files_in_directory(download_dir)
                if len(list_files_after) > len(list_files_before):
                    LOG.info(
                        "Found new files in download directory after click",
                        num_downloaded_files_after=len(list_files_after),
                        download_dir=download_dir,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        workflow_run_id=task.workflow_run_id,
                    )
                    break
                await asyncio.sleep(1)

    except asyncio.TimeoutError:
        LOG.warning(
            "No file to download after click",
            task_id=task.task_id,
            step_id=step.step_id,
            workflow_run_id=task.workflow_run_id,
        )
        return [ActionSuccess(download_triggered=False)]

    # check if there's any file is still downloading
    downloading_files = list_downloading_files_in_directory(download_dir)
    if len(downloading_files) == 0:
        return [ActionSuccess(download_triggered=True)]

    LOG.info(
        "File downloading hasn't completed, wait for a while",
        downloading_files=downloading_files,
        task_id=task.task_id,
        step_id=step.step_id,
        workflow_run_id=task.workflow_run_id,
    )
    try:
        await wait_for_download_finished(downloading_files=downloading_files)
    except DownloadFileMaxWaitingTime as e:
        LOG.warning(
            "There're several long-time downloading files, these files might be broken",
            downloading_files=e.downloading_files,
            task_id=task.task_id,
            step_id=step.step_id,
            workflow_run_id=task.workflow_run_id,
        )

    return [ActionSuccess(download_triggered=True)]


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_input_text_action(
    action: actions.InputTextAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if not action.element_id:
        # This is a CUA type action
        await page.keyboard.type(action.text)
        return [ActionSuccess()]

    dom = DomUtil(scraped_page, page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    skyvern_frame = await SkyvernFrame.create_instance(skyvern_element.get_frame())
    incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame)
    timeout = settings.BROWSER_ACTION_TIMEOUT_MS

    current_text = await get_input_value(skyvern_element.get_tag_name(), skyvern_element.get_locator())
    if current_text == action.text:
        return [ActionSuccess()]

    # before filling text, we need to validate if the element can be filled if it's not one of COMMON_INPUT_TAGS
    tag_name = scraped_page.id_to_element_dict[action.element_id]["tagName"].lower()
    text: str | None = await get_actual_value_of_parameter_if_secret(task, action.text)
    if text is None:
        return [ActionFailure(FailedToFetchSecret())]

    is_totp_value = text == BitwardenConstants.TOTP or text == OnePasswordConstants.TOTP
    is_secret_value = text != action.text

    # dynamically validate the attr, since it could change into enabled after the previous actions
    if await skyvern_element.is_disabled(dynamic=True):
        LOG.warning(
            "Try to input text on a disabled element",
            action_type=action.action_type,
            task_id=task.task_id,
            step_id=step.step_id,
            element_id=skyvern_element.get_id(),
        )
        return [ActionFailure(InteractWithDisabledElement(skyvern_element.get_id()))]

    select_action = SelectOptionAction(
        reasoning=action.reasoning,
        element_id=skyvern_element.get_id(),
        option=SelectOption(label=text),
        intention=action.intention,
    )
    if await skyvern_element.get_selectable():
        LOG.info(
            "Input element is selectable, doing select actions",
            task_id=task.task_id,
            step_id=step.step_id,
            element_id=skyvern_element.get_id(),
            action=action,
        )
        return await handle_select_option_action(select_action, page, scraped_page, task, step)

    incremental_element: list[dict] = []
    auto_complete_hacky_flag: bool = False

    input_or_select_context = await _get_input_or_select_context(
        action=action,
        scraped_page=scraped_page,
        step=step,
    )

    # check if it's selectable
    if (
        not input_or_select_context.is_search_bar  # no need to to trigger selection logic for search bar
        and skyvern_element.get_tag_name() == InteractiveElement.INPUT
        and not await skyvern_element.is_raw_input()
    ):
        await skyvern_element.scroll_into_view()
        # press arrowdown to watch if there's any options popping up
        await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())
        try:
            await skyvern_element.input_clear()
        except Exception:
            LOG.info(
                "Failed to clear up the input, but continue to input",
                task_id=task.task_id,
                step_id=step.step_id,
                element_id=skyvern_element.get_id(),
            )

        try:
            await skyvern_element.press_key("ArrowDown")
        except TimeoutError:
            # sometimes we notice `press_key()` raise a timeout but actually the dropdown is opened.
            LOG.info(
                "Timeout to press ArrowDown to open dropdown, ignore the timeout and continue to execute the action",
                task_id=task.task_id,
                step_id=step.step_id,
                element_id=skyvern_element.get_id(),
                action=action,
            )

        await asyncio.sleep(5)

        incremental_element = await incremental_scraped.get_incremental_element_tree(
            clean_and_remove_element_tree_factory(
                task=task, step=step, check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)]
            ),
        )
        if len(incremental_element) == 0:
            LOG.info(
                "No new element detected, indicating it couldn't be a selectable auto-completion input",
                task_id=task.task_id,
                step_id=step.step_id,
                element_id=skyvern_element.get_id(),
                action=action,
            )
            await incremental_scraped.stop_listen_dom_increment()
        else:
            auto_complete_hacky_flag = True
            try_to_quit_dropdown = True
            try:
                # TODO: we don't select by value for the auto completion detect case

                select_result = await sequentially_select_from_dropdown(
                    action=select_action,
                    input_or_select_context=input_or_select_context,
                    page=page,
                    dom=dom,
                    skyvern_element=skyvern_element,
                    skyvern_frame=skyvern_frame,
                    incremental_scraped=incremental_scraped,
                    step=step,
                    task=task,
                    target_value=text,
                )

                if select_result is not None:
                    if select_result.action_result and select_result.action_result.success:
                        try_to_quit_dropdown = False
                        return [select_result.action_result]

                    if select_result.dropdown_menu is None:
                        try_to_quit_dropdown = False

                    if select_result.action_result is None:
                        LOG.info(
                            "It might not be a selectable auto-completion input, exit the custom selection mode",
                            task_id=task.task_id,
                            step_id=step.step_id,
                            element_id=skyvern_element.get_id(),
                            action=action,
                        )
                    else:
                        LOG.warning(
                            "Custom selection returned an error, continue to input text",
                            task_id=task.task_id,
                            step_id=step.step_id,
                            element_id=skyvern_element.get_id(),
                            action=action,
                            err_msg=select_result.action_result.exception_message,
                        )

            except Exception:
                LOG.warning(
                    "Failed to do custom selection transformed from input action, continue to input text",
                    exc_info=True,
                    task_id=task.task_id,
                    step_id=step.step_id,
                )
                await skyvern_element.scroll_into_view()
            finally:
                if await skyvern_element.is_visible():
                    blocking_element, exist = await skyvern_element.find_blocking_element(
                        dom=dom, incremental_page=incremental_scraped
                    )
                    if blocking_element and exist:
                        LOG.info(
                            "Find a blocking element to the current element, going to blur the blocking element first",
                            task_id=task.task_id,
                            step_id=step.step_id,
                            blocking_element=blocking_element.get_locator(),
                        )
                        if await blocking_element.get_locator().count():
                            await blocking_element.press_key("Escape")
                        if await blocking_element.get_locator().count():
                            await blocking_element.blur()

                if try_to_quit_dropdown and await skyvern_element.is_visible():
                    await skyvern_element.press_key("Escape")
                    await skyvern_element.blur()
                await incremental_scraped.stop_listen_dom_increment()

    ### Start filling text logic
    # check if the element has hidden attribute
    if await skyvern_element.has_hidden_attr():
        return [ActionFailure(InputToInvisibleElement(skyvern_element.get_id()), stop_execution_on_failure=False)]

    # force to move focus back to the element
    await skyvern_element.get_locator().focus(timeout=timeout)

    # check the phone number format when type=tel and the text is not a secret value
    if not is_secret_value and await skyvern_element.get_attr("type") == "tel":
        try:
            text = await check_phone_number_format(
                value=text,
                action=action,
                skyvern_element=skyvern_element,
                scraped_page=scraped_page,
                task=task,
                step=step,
            )
        except Exception:
            LOG.warning(
                "Failed to check the phone number format, using the original text",
                action=action,
                exc_info=True,
            )

    # `Locator.clear()` on a spin button could cause the cursor moving away, and never be back
    if not await skyvern_element.is_spinbtn_input():
        try:
            await skyvern_element.input_clear()
        except TimeoutError:
            LOG.info("None input tag clear timeout", action=action)
            return [ActionFailure(InvalidElementForTextInput(element_id=action.element_id, tag_name=tag_name))]
        except Exception:
            LOG.warning("Failed to clear the input field", action=action, exc_info=True)

            # TODO: some elements are supported to use `locator.press_sequentially()` to fill in the data
            # we need find a better way to detect the attribute in the future
            class_name: str | None = await skyvern_element.get_attr("class")
            if not class_name or "blinking-cursor" not in class_name:
                return [ActionFailure(InvalidElementForTextInput(element_id=action.element_id, tag_name=tag_name))]

            if is_totp_value:
                text = generate_totp_value(task=task, parameter=action.text)
            await skyvern_element.press_fill(text=text)
            return [ActionSuccess()]

    # wait 2s for blocking element to show up
    await asyncio.sleep(2)
    try:
        blocking_element, exist = await skyvern_element.find_blocking_element(
            dom=dom, incremental_page=incremental_scraped
        )
        if blocking_element and exist:
            LOG.warning(
                "Find a blocking element to the current element, going to input on the blocking element",
            )
            skyvern_element = blocking_element
    except Exception:
        LOG.info(
            "Failed to find the blocking element, continue with the original element",
            exc_info=True,
            task_id=task.task_id,
            step_id=step.step_id,
        )

    if is_totp_value:
        LOG.info("Skipping the auto completion logic since it's a TOTP input")
        text = generate_totp_value(task=task, parameter=action.text)
        await skyvern_element.input(text)
        return [ActionSuccess()]

    try:
        # TODO: not sure if this case will trigger auto-completion
        if tag_name not in COMMON_INPUT_TAGS:
            await skyvern_element.input_fill(text)
            return [ActionSuccess()]

        if len(text) == 0:
            return [ActionSuccess()]

        if not await skyvern_element.is_raw_input():
            if await skyvern_element.is_auto_completion_input() or input_or_select_context.is_location_input:
                if result := await input_or_auto_complete_input(
                    input_or_select_context=input_or_select_context,
                    page=page,
                    dom=dom,
                    text=text,
                    skyvern_element=skyvern_element,
                    step=step,
                    task=task,
                ):
                    auto_complete_hacky_flag = False
                    return [result]

        await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())

        try:
            await skyvern_element.input_sequentially(text=text)
        finally:
            incremental_element = await incremental_scraped.get_incremental_element_tree(
                clean_and_remove_element_tree_factory(
                    task=task, step=step, check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)]
                ),
            )
            if len(incremental_element) > 0:
                auto_complete_hacky_flag = True
            await incremental_scraped.stop_listen_dom_increment()

        return [ActionSuccess()]
    except Exception as e:
        LOG.exception(
            "Failed to input the value or finish the auto completion",
            task_id=task.task_id,
            step_id=step.step_id,
        )
        raise e
    finally:
        # HACK: force to finish missing auto completion input
        if auto_complete_hacky_flag and await skyvern_element.is_visible() and not await skyvern_element.is_raw_input():
            LOG.debug(
                "Trigger input-selection hack, pressing Tab to choose one",
                action=action,
                task_id=task.task_id,
                step_id=step.step_id,
            )
            await skyvern_element.press_key("Tab")


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_upload_file_action(
    action: actions.UploadFileAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if not action.file_url:
        LOG.warning("InputFileAction has no file_url", action=action)
        return [ActionFailure(MissingFileUrl())]
    # ************************************************************************************************************** #
    # After this point if the file_url is a secret, it will be replaced with the actual value
    # In order to make sure we don't log the secret value, we log the action with the original value action.file_url
    # ************************************************************************************************************** #
    file_url = await get_actual_value_of_parameter_if_secret(task, action.file_url)
    decoded_url = urllib.parse.unquote(file_url)
    if (
        file_url not in str(task.navigation_payload)
        and file_url not in str(task.navigation_goal)
        and decoded_url not in str(task.navigation_payload)
        and decoded_url not in str(task.navigation_goal)
    ):
        LOG.warning(
            "LLM might be imagining the file url, which is not in navigation payload",
            action=action,
            file_url=action.file_url,
        )
        return [ActionFailure(ImaginaryFileUrl(action.file_url))]

    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)

    # dynamically validate the attr, since it could change into enabled after the previous actions
    if await skyvern_element.is_disabled(dynamic=True):
        LOG.warning(
            "Try to upload file on a disabled element",
            action_type=action.action_type,
            task_id=task.task_id,
            step_id=step.step_id,
            element_id=skyvern_element.get_id(),
        )
        return [ActionFailure(InteractWithDisabledElement(skyvern_element.get_id()))]

    locator = skyvern_element.locator

    file_path = await download_file(file_url)
    is_file_input = await skyvern_element.is_file_input()

    if is_file_input:
        LOG.info("Taking UploadFileAction. Found file input tag", action=action)
        if file_path:
            await locator.set_input_files(
                file_path,
                timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
            )

            # Sleep for 10 seconds after uploading a file to let the page process it
            await asyncio.sleep(10)

            return [ActionSuccess()]
        else:
            return [ActionFailure(Exception(f"Failed to download file from {action.file_url}"))]
    else:
        LOG.info("Taking UploadFileAction. Found non file input tag", action=action)
        # treat it as a click action
        action.is_upload_file_tag = False
        return await chain_click(
            task,
            scraped_page,
            page,
            action,
            skyvern_element,
            timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
        )


# This function is deprecated. Downloads are handled by the click action handler now.
@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_download_file_action(
    action: actions.DownloadFileAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)

    file_name = f"{action.file_name or uuid.uuid4()}"
    full_file_path = f"{REPO_ROOT_DIR}/downloads/{task.workflow_run_id or task.task_id}/{file_name}"
    try:
        # Start waiting for the download
        async with page.expect_download() as download_info:
            await asyncio.sleep(0.3)

            locator = skyvern_element.locator
            await locator.click(
                timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
                modifiers=["Alt"],
            )

        download = await download_info.value

        # Create download folders if they don't exist
        download_folder = f"{REPO_ROOT_DIR}/downloads/{task.workflow_run_id or task.task_id}"
        os.makedirs(download_folder, exist_ok=True)
        # Wait for the download process to complete and save the downloaded file
        await download.save_as(full_file_path)
    except Exception as e:
        LOG.exception(
            "DownloadFileAction: Failed to download file",
            action=action,
            full_file_path=full_file_path,
        )
        return [ActionFailure(e)]

    return [ActionSuccess(data={"file_path": full_file_path})]


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_null_action(
    action: actions.NullAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    return [ActionSuccess()]


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_select_option_action(
    action: actions.SelectOptionAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    dom = DomUtil(scraped_page, page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)

    tag_name = skyvern_element.get_tag_name()
    element_dict = scraped_page.id_to_element_dict[action.element_id]
    LOG.info(
        "SelectOptionAction",
        action=action,
        tag_name=tag_name,
        element_dict=element_dict,
    )

    # Handle the edge case:
    # Sometimes our custom select logic could fail, and leaving the dropdown being opened.
    # Confirm if the select action is on the custom option element
    if await skyvern_element.is_custom_option():
        click_action = ClickAction(element_id=action.element_id)
        return await chain_click(task, scraped_page, page, click_action, skyvern_element)

    if not await skyvern_element.is_selectable():
        # 1. find from children
        # TODO: 2. find from siblings and their children
        LOG.info(
            "Element is not selectable, try to find the selectable element in the children",
            tag_name=tag_name,
            action=action,
        )

        selectable_child: SkyvernElement | None = None
        try:
            selectable_child = await skyvern_element.find_selectable_child(dom=dom)
        except Exception as e:
            LOG.error(
                "Failed to find selectable element in children",
                exc_info=True,
                tag_name=tag_name,
                action=action,
            )
            return [ActionFailure(ErrFoundSelectableElement(action.element_id, e))]

        if selectable_child:
            LOG.info(
                "Found selectable element in the children",
                tag_name=selectable_child.get_tag_name(),
                element_id=selectable_child.get_id(),
            )
            select_action = SelectOptionAction(
                reasoning=action.reasoning,
                element_id=selectable_child.get_id(),
                option=action.option,
                intention=action.intention,
            )
            action = select_action
            skyvern_element = selectable_child

    # dynamically validate the attr, since it could change into enabled after the previous actions
    if await skyvern_element.is_disabled(dynamic=True):
        LOG.warning(
            "Try to select on a disabled element",
            action_type=action.action_type,
            task_id=task.task_id,
            step_id=step.step_id,
            element_id=skyvern_element.get_id(),
        )
        return [ActionFailure(InteractWithDisabledElement(skyvern_element.get_id()))]

    if skyvern_element.get_tag_name() == InteractiveElement.SELECT:
        LOG.info(
            "SelectOptionAction is on <select>",
            action=action,
            task_id=task.task_id,
            step_id=step.step_id,
        )

        try:
            blocking_element, exist = await skyvern_element.find_blocking_element(dom=dom)
        except Exception:
            LOG.warning(
                "Failed to find the blocking element, continue to select on the original <select>",
                task_id=task.task_id,
                step_id=step.step_id,
                exc_info=True,
            )
            return await normal_select(
                action=action, skyvern_element=skyvern_element, builder=dom.scraped_page, task=task, step=step
            )

        if not exist:
            return await normal_select(
                action=action, skyvern_element=skyvern_element, builder=dom.scraped_page, task=task, step=step
            )

        if blocking_element is None:
            LOG.info(
                "Try to scroll the element into view, then detecting the blocking element",
                step_id=step.step_id,
            )
            try:
                await skyvern_element.scroll_into_view()
                blocking_element, exist = await skyvern_element.find_blocking_element(dom=dom)
            except Exception:
                LOG.warning(
                    "Failed to find the blocking element when scrolling into view, fallback to normal select",
                    action=action,
                    step_id=step.step_id,
                    exc_info=True,
                )
                return await normal_select(
                    action=action, skyvern_element=skyvern_element, builder=dom.scraped_page, task=task, step=step
                )

        if not exist or blocking_element is None:
            return await normal_select(
                action=action, skyvern_element=skyvern_element, builder=dom.scraped_page, task=task, step=step
            )
        LOG.info(
            "<select> is blocked by another element, going to select on the blocking element",
            task_id=task.task_id,
            step_id=step.step_id,
            blocking_element=blocking_element.get_id(),
        )
        select_action = SelectOptionAction(
            reasoning=action.reasoning,
            element_id=blocking_element.get_id(),
            option=action.option,
            intention=action.intention,
        )
        action = select_action
        skyvern_element = blocking_element

    if await skyvern_element.is_checkbox():
        LOG.info(
            "SelectOptionAction is on <input> checkbox",
            action=action,
            task_id=task.task_id,
            step_id=step.step_id,
        )
        check_action = CheckboxAction(element_id=action.element_id, is_checked=True)
        return await handle_checkbox_action(check_action, page, scraped_page, task, step)

    if await skyvern_element.is_radio():
        LOG.info(
            "SelectOptionAction is on <input> radio",
            action=action,
            task_id=task.task_id,
            step_id=step.step_id,
        )
        click_action = ClickAction(element_id=action.element_id)
        return await chain_click(task, scraped_page, page, click_action, skyvern_element)

    # FIXME: maybe there's a case where <input type="button"> could trigger dropdown menu?
    if await skyvern_element.is_btn_input():
        LOG.info(
            "SelectOptionAction is on <input> button",
            action=action,
            task_id=task.task_id,
            step_id=step.step_id,
        )
        click_action = ClickAction(element_id=action.element_id)
        return await chain_click(task, scraped_page, page, click_action, skyvern_element)

    LOG.info(
        "Trigger custom select",
        action=action,
        element_id=skyvern_element.get_id(),
    )

    timeout = settings.BROWSER_ACTION_TIMEOUT_MS
    skyvern_frame = await SkyvernFrame.create_instance(skyvern_element.get_frame())
    incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame)
    is_open = False
    suggested_value: str | None = None
    results: list[ActionResult] = []

    try:
        await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())
        await skyvern_element.scroll_into_view()

        await skyvern_element.click(page=page, dom=dom, timeout=timeout)
        # wait 5s for options to load
        await asyncio.sleep(5)

        incremental_element = await incremental_scraped.get_incremental_element_tree(
            clean_and_remove_element_tree_factory(
                task=task, step=step, check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)]
            ),
        )

        if len(incremental_element) == 0 and skyvern_element.get_tag_name() == InteractiveElement.INPUT:
            LOG.info(
                "No incremental elements detected for the input element, trying to press Arrowdown to trigger the dropdown",
                element_id=skyvern_element.get_id(),
                task_id=task.task_id,
                step_id=step.step_id,
            )
            await skyvern_element.scroll_into_view()
            await skyvern_element.press_key("ArrowDown")
            # wait 5s for options to load
            await asyncio.sleep(5)
            incremental_element = await incremental_scraped.get_incremental_element_tree(
                clean_and_remove_element_tree_factory(
                    task=task, step=step, check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)]
                ),
            )

        input_or_select_context = await _get_input_or_select_context(
            action=action, scraped_page=scraped_page, step=step
        )

        if len(incremental_element) == 0:
            LOG.info(
                "No incremental elements detected by MutationObserver, using re-scraping the page to find the match element"
            )
            results.append(
                await select_from_emerging_elements(
                    current_element_id=skyvern_element.get_id(),
                    options=CustomSelectPromptOptions(
                        is_date_related=input_or_select_context.is_date_related or False,
                        field_information=input_or_select_context.intention or input_or_select_context.field or "",
                        required_field=input_or_select_context.is_required or False,
                        target_value=action.option.label or action.option.value or "",
                    ),
                    page=page,
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                )
            )
            return results

        is_open = True
        # TODO: support sequetially select from dropdown by value, just support single select now
        result = await sequentially_select_from_dropdown(
            action=action,
            input_or_select_context=input_or_select_context,
            page=page,
            dom=dom,
            skyvern_element=skyvern_element,
            skyvern_frame=skyvern_frame,
            incremental_scraped=incremental_scraped,
            step=step,
            task=task,
            force_select=True,
            target_value=action.option.label or action.option.value or "",
        )
        # force_select won't return None result
        assert result is not None
        assert result.action_result is not None
        results.append(result.action_result)
        if isinstance(result.action_result, ActionSuccess) or result.value is None:
            return results
        suggested_value = result.value

    except Exception as e:
        LOG.exception("Custom select error")
        results.append(ActionFailure(exception=e))
        return results
    finally:
        if (
            await skyvern_element.is_visible()
            and is_open
            and len(results) > 0
            and not isinstance(results[-1], ActionSuccess)
        ):
            await skyvern_element.scroll_into_view()
            await skyvern_element.coordinate_click(page=page)
            await skyvern_element.press_key("Escape")
        is_open = False
        await skyvern_element.blur()
        await incremental_scraped.stop_listen_dom_increment()

    LOG.info(
        "Try to select by value in custom select",
        element_id=skyvern_element.get_id(),
        value=suggested_value,
        task_id=task.task_id,
        step_id=step.step_id,
    )
    try:
        await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())
        timeout = settings.BROWSER_ACTION_TIMEOUT_MS
        await skyvern_element.scroll_into_view()

        try:
            await skyvern_element.get_locator().click(timeout=timeout)
        except Exception:
            LOG.info(
                "fail to open dropdown by clicking, try to press arrow down to open",
                element_id=skyvern_element.get_id(),
                task_id=task.task_id,
                step_id=step.step_id,
            )
            await skyvern_element.scroll_into_view()
            await skyvern_element.press_key("ArrowDown")
        await asyncio.sleep(5)
        is_open = True

        result = await select_from_dropdown_by_value(
            value=suggested_value,
            page=page,
            dom=dom,
            skyvern_element=skyvern_element,
            skyvern_frame=skyvern_frame,
            incremental_scraped=incremental_scraped,
            task=task,
            step=step,
        )
        results.append(result)
        return results

    except Exception as e:
        LOG.exception("Custom select by value error")
        results.append(ActionFailure(exception=e))
        return results

    finally:
        if (
            await skyvern_element.is_visible()
            and is_open
            and len(results) > 0
            and not isinstance(results[-1], ActionSuccess)
        ):
            await skyvern_element.scroll_into_view()
            await skyvern_element.coordinate_click(page=page)
            await skyvern_element.press_key("Escape")
        is_open = False
        await skyvern_element.blur()
        await incremental_scraped.stop_listen_dom_increment()


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_checkbox_action(
    action: actions.CheckboxAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    """
    ******* NOT REGISTERED *******
    This action causes more harm than it does good.
    It frequently mis-behaves, or gets stuck in click loops.
    Treating checkbox actions as click actions seem to perform way more reliably
    Developers who tried this and failed: 2 (Suchintan and Shu 😂)
    """

    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    locator = skyvern_element.locator

    if action.is_checked:
        await locator.check(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
    else:
        await locator.uncheck(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)

    # TODO (suchintan): Why does checking the label work, but not the actual input element?
    return [ActionSuccess()]


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_wait_action(
    action: actions.WaitAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    await asyncio.sleep(action.seconds)
    return [ActionFailure(exception=Exception("Wait action is treated as a failure"))]


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_terminate_action(
    action: actions.TerminateAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    return [ActionSuccess()]


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_complete_action(
    action: actions.CompleteAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if not action.verified and task.navigation_goal:
        LOG.info(
            "CompleteAction hasn't been verified, going to verify the user goal",
            task_id=task.task_id,
            step_id=step.step_id,
            workflow_run_id=task.workflow_run_id,
        )
        try:
            verification_result = await app.agent.complete_verify(page, scraped_page, task, step)
        except Exception as e:
            LOG.exception(
                "Failed to verify the complete action",
                task_id=task.task_id,
                step_id=step.step_id,
                workflow_run_id=task.workflow_run_id,
            )
            return [ActionFailure(exception=e)]

        if not verification_result.user_goal_achieved:
            return [ActionFailure(exception=IllegitComplete(data={"error": verification_result.thoughts}))]

        LOG.info(
            "CompleteAction has been verified successfully",
            task_id=task.task_id,
            step_id=step.step_id,
            workflow_run_id=task.workflow_run_id,
        )
        action.verified = True
        if not task.data_extraction_goal and verification_result.thoughts:
            await app.DATABASE.update_task(
                task.task_id,
                organization_id=task.organization_id,
                extracted_information=verification_result.thoughts,
            )

    return [ActionSuccess()]


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_extract_action(
    action: actions.ExtractAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    extracted_data = None
    if task.data_extraction_goal:
        scrape_action_result = await extract_information_for_navigation_goal(
            scraped_page=scraped_page,
            task=task,
            step=step,
        )
        extracted_data = scrape_action_result.scraped_data
        return [ActionSuccess(data=extracted_data)]
    else:
        LOG.warning("No data extraction goal, skipping extract action", step_id=step.step_id)
        return [ActionFailure(exception=Exception("No data extraction goal"))]


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_scroll_action(
    action: actions.ScrollAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if action.x and action.y:
        await page.mouse.move(action.x, action.y)
    await page.evaluate(f"window.scrollBy({action.scroll_x}, {action.scroll_y})")
    return [ActionSuccess()]


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_keypress_action(
    action: actions.KeypressAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    updated_keys = []
    for key in action.keys:
        key_lower_case = key.lower()
        if key_lower_case in ("enter", "return"):
            updated_keys.append("Enter")
        elif key_lower_case == "space":
            updated_keys.append(" ")
        elif key_lower_case == "ctrl":
            updated_keys.append("Control")
        elif key_lower_case == "backspace":
            updated_keys.append("Backspace")
        elif key_lower_case == "pagedown":
            updated_keys.append("PageDown")
        elif key_lower_case == "pageup":
            updated_keys.append("PageUp")
        elif key_lower_case == "tab":
            updated_keys.append("Tab")
        elif key_lower_case == "shift":
            updated_keys.append("Shift")
        elif key_lower_case in ("arrowleft", "left"):
            updated_keys.append("ArrowLeft")
        elif key_lower_case in ("arrowright", "right"):
            updated_keys.append("ArrowRight")
        elif key_lower_case in ("arrowup", "up"):
            updated_keys.append("ArrowUp")
        elif key_lower_case in ("arrowdown", "down"):
            updated_keys.append("ArrowDown")
        elif key_lower_case == "home":
            updated_keys.append("Home")
        elif key_lower_case == "end":
            updated_keys.append("End")
        elif key_lower_case == "delete":
            updated_keys.append("Delete")
        elif key_lower_case == "ecs":
            updated_keys.append("Escape")
        elif key_lower_case == "alt":
            updated_keys.append("Alt")
        elif key_lower_case.startswith("f") and key_lower_case[1:].isdigit():
            # Handle function keys: f1 -> F1, f5 -> F5, etc.
            updated_keys.append(key_lower_case.upper())
        else:
            updated_keys.append(key)
    keypress_str = "+".join(updated_keys)
    if action.hold:
        await page.keyboard.down(keypress_str)
        await asyncio.sleep(action.duration)
        await page.keyboard.up(keypress_str)
    else:
        await page.keyboard.press(keypress_str)
    return [ActionSuccess()]


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_move_action(
    action: actions.MoveAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    await page.mouse.move(action.x, action.y)
    return [ActionSuccess()]


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_drag_action(
    action: actions.DragAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if action.start_x and action.start_y:
        await page.mouse.move(action.start_x, action.start_y)
    await page.mouse.down()
    for point in action.path:
        x, y = point[0], point[1]
        await page.mouse.move(x, y)
    await page.mouse.up()
    return [ActionSuccess()]


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_verification_code_action(
    action: actions.VerificationCodeAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    LOG.info(
        "Setting verification code in skyvern context",
        task_id=task.task_id,
        step_id=step.step_id,
        verification_code=action.verification_code,
    )
    current_context = skyvern_context.ensure_context()
    current_context.totp_codes[task.task_id] = action.verification_code
    return [ActionSuccess()]


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def handle_left_mouse_action(
    action: actions.LeftMouseAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if action.x and action.y:
        await page.mouse.move(action.x, action.y)
    if action.direction == "down":
        await page.mouse.down()
    elif action.direction == "up":
        await page.mouse.up()
    return [ActionSuccess()]


ActionHandler.register_action_type(ActionType.SOLVE_CAPTCHA, handle_solve_captcha_action)
ActionHandler.register_action_type(ActionType.CLICK, handle_click_action)
ActionHandler.register_action_type(ActionType.INPUT_TEXT, handle_input_text_action)
ActionHandler.register_action_type(ActionType.UPLOAD_FILE, handle_upload_file_action)
# ActionHandler.register_action_type(ActionType.DOWNLOAD_FILE, handle_download_file_action)
ActionHandler.register_action_type(ActionType.NULL_ACTION, handle_null_action)
ActionHandler.register_action_type(ActionType.SELECT_OPTION, handle_select_option_action)
ActionHandler.register_action_type(ActionType.WAIT, handle_wait_action)
ActionHandler.register_action_type(ActionType.TERMINATE, handle_terminate_action)
ActionHandler.register_action_type(ActionType.COMPLETE, handle_complete_action)
ActionHandler.register_action_type(ActionType.EXTRACT, handle_extract_action)
ActionHandler.register_action_type(ActionType.SCROLL, handle_scroll_action)
ActionHandler.register_action_type(ActionType.KEYPRESS, handle_keypress_action)
ActionHandler.register_action_type(ActionType.MOVE, handle_move_action)
ActionHandler.register_action_type(ActionType.DRAG, handle_drag_action)
ActionHandler.register_action_type(ActionType.VERIFICATION_CODE, handle_verification_code_action)
ActionHandler.register_action_type(ActionType.LEFT_MOUSE, handle_left_mouse_action)


async def get_actual_value_of_parameter_if_secret(task: Task, parameter: str) -> Any:
    """
    Get the actual value of a parameter if it's a secret. If it's not a secret, return the parameter value as is.

    Just return the parameter value if the task isn't a workflow's task.

    This is only used for InputTextAction, UploadFileAction, and ClickAction (if it has a file_url).
    """
    if task.workflow_run_id is None:
        return parameter

    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(task.workflow_run_id)
    secret_value = workflow_run_context.get_original_secret_value_or_none(parameter)
    return secret_value if secret_value is not None else parameter


def generate_totp_value(task: Task, parameter: str) -> str:
    if task.workflow_run_id is None:
        return parameter

    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(task.workflow_run_id)
    totp_secret_key = workflow_run_context.totp_secret_value_key(parameter)
    totp_secret = workflow_run_context.get_original_secret_value_or_none(totp_secret_key)
    totp_secret_no_whitespace = "".join(totp_secret.split())
    return pyotp.TOTP(totp_secret_no_whitespace).now()


async def chain_click(
    task: Task,
    scraped_page: ScrapedPage,
    page: Page,
    action: ClickAction | UploadFileAction,
    skyvern_element: SkyvernElement,
    timeout: int = settings.BROWSER_ACTION_TIMEOUT_MS,
) -> List[ActionResult]:
    # Add a defensive page handler here in case a click action opens a file chooser.
    # This automatically dismisses the dialog
    # File choosers are impossible to close if you don't expect one. Instead of dealing with it, close it!

    dom = DomUtil(scraped_page=scraped_page, page=page)
    locator = skyvern_element.locator
    # TODO (suchintan): This should likely result in an ActionFailure -- we can figure out how to do this later!
    LOG.info("Chain click starts", action=action, locator=locator)
    file: list[str] | str = []
    if action.file_url:
        file_url = await get_actual_value_of_parameter_if_secret(task, action.file_url)
        file = await handler_utils.download_file(file_url, action.model_dump())

    is_filechooser_trigger = False

    async def fc_func(fc: FileChooser) -> None:
        nonlocal is_filechooser_trigger
        is_filechooser_trigger = True
        await fc.set_files(files=file)

    page.on("filechooser", fc_func)
    LOG.info("Registered file chooser listener", action=action, path=file)

    """
    Clicks on an element identified by the css and its parent if failed.
    :param css: css of the element to click
    """
    try:
        if not await skyvern_element.navigate_to_a_href(page=page):
            await locator.click(timeout=timeout)
            LOG.info("Chain click: main element click succeeded", action=action, locator=locator)
        return [ActionSuccess()]

    except Exception as e:
        action_results: list[ActionResult] = [ActionFailure(FailToClick(action.element_id, msg=str(e)))]

        if skyvern_element.get_tag_name() == "label":
            try:
                LOG.info(
                    "Chain click: it's a label element. going to try for-click",
                    task_id=task.task_id,
                    action=action,
                    element=str(skyvern_element),
                    locator=locator,
                )
                if bound_element := await skyvern_element.find_label_for(dom=dom):
                    await bound_element.get_locator().click(timeout=timeout)
                    action_results.append(ActionSuccess())
                    return action_results
            except Exception as e:
                action_results.append(ActionFailure(FailToClick(action.element_id, anchor="for", msg=str(e))))

            try:
                # sometimes the element is the direct children of the label, instead of using for="xx" attribute
                # since it's a click action, the target element we're searching should only be INPUT
                LOG.info(
                    "Chain click: it's a label element. going to check for input of the direct children",
                    task_id=task.task_id,
                    action=action,
                    element=str(skyvern_element),
                    locator=locator,
                )
                if bound_element := await skyvern_element.find_element_in_label_children(
                    dom=dom, element_type=InteractiveElement.INPUT
                ):
                    await bound_element.get_locator().click(timeout=timeout)
                    action_results.append(ActionSuccess())
                    return action_results
            except Exception as e:
                action_results.append(
                    ActionFailure(FailToClick(action.element_id, anchor="direct_children", msg=str(e)))
                )

        else:
            try:
                LOG.info(
                    "Chain click: it's a non-label element. going to find the bound label element by attribute id and click",
                    task_id=task.task_id,
                    action=action,
                    element=str(skyvern_element),
                    locator=locator,
                )
                if bound_locator := await skyvern_element.find_bound_label_by_attr_id():
                    await bound_locator.click(timeout=timeout)
                    action_results.append(ActionSuccess())
                    return action_results
            except Exception as e:
                action_results.append(ActionFailure(FailToClick(action.element_id, anchor="attr_id", msg=str(e))))

            try:
                # sometimes the element is the direct children of the label, instead of using for="xx" attribute
                # so we check the direct parent if it's a label element
                LOG.info(
                    "Chain click: it's a non-label element. going to find the bound label element by direct parent",
                    task_id=task.task_id,
                    action=action,
                    element=str(skyvern_element),
                    locator=locator,
                )
                if bound_locator := await skyvern_element.find_bound_label_by_direct_parent():
                    await bound_locator.click(timeout=timeout)
                    action_results.append(ActionSuccess())
                    return action_results
            except Exception as e:
                action_results.append(ActionFailure(FailToClick(action.element_id, anchor="direct_parent", msg=str(e))))

        if not await skyvern_element.is_visible():
            LOG.info(
                "Chain click: exit since the element is not visible on the page anymore",
                task_id=task.task_id,
                action=action,
                element=str(skyvern_element),
                locator=locator,
            )
            return action_results

        blocking_element, blocked = await skyvern_element.find_blocking_element(
            dom=DomUtil(scraped_page=scraped_page, page=page)
        )
        if blocking_element is None:
            if not blocked:
                LOG.info(
                    "Chain click: exit since the element is not blocking by any element",
                    task_id=task.task_id,
                    action=action,
                    element=str(skyvern_element),
                    locator=locator,
                )
                return action_results

            try:
                LOG.info(
                    "Chain click: element is blocked by an non-interactable element, try to click by the coordinates",
                    task_id=task.task_id,
                    action=action,
                    element=str(skyvern_element),
                    locator=locator,
                )
                await skyvern_element.coordinate_click(page=page)
                action_results.append(ActionSuccess())
                return action_results
            except Exception as e:
                action_results.append(
                    ActionFailure(FailToClick(action.element_id, anchor="coordinate_click", msg=str(e)))
                )

            LOG.info(
                "Chain click: element is blocked by an non-interactable element, going to use javascript click instead of playwright click",
                task_id=task.task_id,
                action=action,
                element=str(skyvern_element),
                locator=locator,
            )
            try:
                await skyvern_element.click_in_javascript()
                action_results.append(ActionSuccess())
                return action_results
            except Exception as e:
                action_results.append(ActionFailure(FailToClick(action.element_id, anchor="self_js", msg=str(e))))
                return action_results

        try:
            LOG.debug(
                "Chain click: verifying the blocking element is parent or sibling of the target element",
                task_id=task.task_id,
                action=action,
                element=str(blocking_element),
                locator=locator,
            )
            if await blocking_element.is_parent_of(
                await skyvern_element.get_element_handler()
            ) or await blocking_element.is_sibling_of(await skyvern_element.get_element_handler()):
                LOG.info(
                    "Chain click: element is blocked by other elements, going to click on the blocking element",
                    task_id=task.task_id,
                    action=action,
                    element=str(blocking_element),
                    locator=locator,
                )

                await blocking_element.get_locator().click(timeout=timeout)
                action_results.append(ActionSuccess())
                return action_results
        except Exception as e:
            action_results.append(ActionFailure(FailToClick(action.element_id, anchor="blocking_element", msg=str(e))))

        return action_results
    finally:
        LOG.info("Remove file chooser listener", action=action)

        # Sleep for 15 seconds after uploading a file to let the page process it
        # Removing this breaks file uploads using the filechooser
        # KEREM DO NOT REMOVE
        if file:
            await asyncio.sleep(15)
        page.remove_listener("filechooser", fc_func)

        if action.file_url and not is_filechooser_trigger:
            LOG.warning(
                "Action has file_url, but filechoose even hasn't been triggered. Upload file attempt seems to fail",
                action=action,
            )
            return [ActionFailure(WrongElementToUploadFile(action.element_id))]


@TraceManager.traced_async(ignore_inputs=["context", "page", "dom", "text", "skyvern_element", "preserved_elements"])
async def choose_auto_completion_dropdown(
    context: InputOrSelectContext,
    page: Page,
    dom: DomUtil,
    text: str,
    skyvern_element: SkyvernElement,
    step: Step,
    task: Task,
    preserved_elements: list[dict] | None = None,
    relevance_threshold: float = 0.8,
) -> AutoCompletionResult:
    preserved_elements = preserved_elements or []
    clear_input = True
    result = AutoCompletionResult()

    current_frame = skyvern_element.get_frame()
    skyvern_frame = await SkyvernFrame.create_instance(current_frame)
    incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame)
    await incremental_scraped.start_listen_dom_increment(await skyvern_element.get_element_handler())

    try:
        await skyvern_element.press_fill(text)
        # wait for new elemnts to load
        await asyncio.sleep(5)
        incremental_element = await incremental_scraped.get_incremental_element_tree(
            clean_and_remove_element_tree_factory(
                task=task, step=step, check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)]
            ),
        )

        # check if elements in preserve list are still on the page
        confirmed_preserved_list: list[dict] = []
        for element in preserved_elements:
            element_id = element.get("id")
            if not element_id:
                continue
            locator = current_frame.locator(f'[{SKYVERN_ID_ATTR}="{element_id}"]')
            cnt = await locator.count()
            if cnt == 0:
                continue

            element_handler = await locator.element_handle(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
            if not element_handler:
                continue

            current_element = await skyvern_frame.parse_element_from_html(
                skyvern_element.get_frame_id(),
                element_handler,
                skyvern_element.is_interactable(),
            )
            confirmed_preserved_list.append(current_element)

        if len(confirmed_preserved_list) > 0:
            confirmed_preserved_list = await app.AGENT_FUNCTION.cleanup_element_tree_factory(task=task, step=step)(
                skyvern_frame.get_frame(), skyvern_frame.get_frame().url, copy.deepcopy(confirmed_preserved_list)
            )
            confirmed_preserved_list = trim_element_tree(copy.deepcopy(confirmed_preserved_list))

        incremental_element.extend(confirmed_preserved_list)

        result.incremental_elements = copy.deepcopy(incremental_element)
        if len(incremental_element) == 0:
            raise NoIncrementalElementFoundForAutoCompletion(element_id=skyvern_element.get_id(), text=text)

        cleaned_incremental_element = remove_duplicated_HTML_element(incremental_element)
        html = incremental_scraped.build_html_tree(cleaned_incremental_element)
        auto_completion_confirm_prompt = prompt_engine.load_prompt(
            "auto-completion-choose-option",
            is_search=context.is_search_bar,
            field_information=context.field if not context.intention else context.intention,
            filled_value=text,
            navigation_goal=task.navigation_goal,
            navigation_payload_str=json.dumps(task.navigation_payload),
            elements=html,
            local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
        )
        LOG.info(
            "Confirm if it's an auto completion dropdown",
            step_id=step.step_id,
            task_id=task.task_id,
        )
        json_response = await app.SECONDARY_LLM_API_HANDLER(
            prompt=auto_completion_confirm_prompt, step=step, prompt_name="auto-completion-choose-option"
        )
        element_id = json_response.get("id", "")
        relevance_float = json_response.get("relevance_float", 0)
        if json_response.get("direct_searching", False):
            LOG.info(
                "Decided to directly search with the current value",
                value=text,
                step_id=step.step_id,
                task_id=task.task_id,
            )
            await skyvern_element.press_key("Enter")
            return result

        if not element_id:
            reasoning = json_response.get("reasoning")
            raise NoSuitableAutoCompleteOption(reasoning=reasoning, target_value=text)

        if relevance_float < relevance_threshold:
            LOG.info(
                f"The closest option doesn't meet the condition(relevance_float>={relevance_threshold})",
                element_id=element_id,
                relevance_float=relevance_float,
            )
            reasoning = json_response.get("reasoning")
            raise NoAutoCompleteOptionMeetCondition(
                reasoning=reasoning,
                required_relevance=relevance_threshold,
                target_value=text,
                closest_relevance=relevance_float,
            )

        LOG.info(
            "Find a suitable option to choose",
            element_id=element_id,
            step_id=step.step_id,
            task_id=task.task_id,
        )

        locator = current_frame.locator(f'[{SKYVERN_ID_ATTR}="{element_id}"]')
        if await locator.count() == 0:
            raise MissingElement(element_id=element_id)

        await locator.click(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
        clear_input = False
        return result
    except Exception as e:
        LOG.info(
            "Failed to choose the auto completion dropdown",
            exc_info=True,
            input_value=text,
            task_id=task.task_id,
            step_id=step.step_id,
        )
        result.action_result = ActionFailure(exception=e)
        return result
    finally:
        await incremental_scraped.stop_listen_dom_increment()
        if clear_input and await skyvern_element.is_visible():
            await skyvern_element.input_clear()


def remove_duplicated_HTML_element(elements: list[dict]) -> list[dict]:
    cache_map = set()
    new_elements: list[dict] = []
    for element in elements:
        key = hash_element(element=element)
        if key in cache_map:
            continue
        cache_map.add(key)
        new_elements.append(element)
    return new_elements


async def input_or_auto_complete_input(
    input_or_select_context: InputOrSelectContext,
    page: Page,
    dom: DomUtil,
    text: str,
    skyvern_element: SkyvernElement,
    step: Step,
    task: Task,
) -> ActionResult | None:
    LOG.info(
        "Trigger auto completion",
        task_id=task.task_id,
        step_id=step.step_id,
        element_id=skyvern_element.get_id(),
    )

    # 1. press the original text to see if there's a match
    # 2. call LLM to find 5 potential values based on the orginal text
    # 3. try each potential values from #2
    # 4. call LLM to tweak the original text according to the information from #3, then start #1 again

    # FIXME: try the whole loop for once now, to speed up skyvern
    MAX_AUTO_COMPLETE_ATTEMP = 1
    current_attemp = 0
    current_value = text
    result = AutoCompletionResult()

    while current_attemp < MAX_AUTO_COMPLETE_ATTEMP:
        current_attemp += 1
        whole_new_elements: list[dict] = []
        tried_values: list[str] = []

        LOG.info(
            "Try the potential value for auto completion",
            step_id=step.step_id,
            task_id=task.task_id,
            input_value=current_value,
        )
        result = await choose_auto_completion_dropdown(
            context=input_or_select_context,
            page=page,
            dom=dom,
            text=current_value,
            preserved_elements=result.incremental_elements,
            skyvern_element=skyvern_element,
            step=step,
            task=task,
        )
        if isinstance(result.action_result, ActionSuccess):
            return ActionSuccess()

        if input_or_select_context.is_search_bar:
            LOG.info(
                "Stop generating potential values for the auto-completion since it's a search bar",
                context=input_or_select_context,
                step_id=step.step_id,
                task_id=task.task_id,
            )
            return None

        tried_values.append(current_value)
        whole_new_elements.extend(result.incremental_elements)

        field_information = (
            input_or_select_context.field
            if not input_or_select_context.intention
            else input_or_select_context.intention
        )

        prompt = prompt_engine.load_prompt(
            "auto-completion-potential-answers",
            potential_value_count=AUTO_COMPLETION_POTENTIAL_VALUES_COUNT,
            field_information=field_information,
            current_value=current_value,
            navigation_goal=task.navigation_goal,
            navigation_payload_str=json.dumps(task.navigation_payload),
            local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
        )

        LOG.info(
            "Ask LLM to give potential values based on the current value",
            current_value=current_value,
            step_id=step.step_id,
            task_id=task.task_id,
            potential_value_count=AUTO_COMPLETION_POTENTIAL_VALUES_COUNT,
        )
        json_respone = await app.SECONDARY_LLM_API_HANDLER(
            prompt=prompt, step=step, prompt_name="auto-completion-potential-answers"
        )
        values: list[dict] = json_respone.get("potential_values", [])

        for each_value in values:
            value: str = each_value.get("value", "")
            if not value:
                LOG.info(
                    "Empty potential value, skip this attempt",
                    step_id=step.step_id,
                    task_id=task.task_id,
                    value=each_value,
                )
                continue
            LOG.info(
                "Try the potential value for auto completion",
                step_id=step.step_id,
                task_id=task.task_id,
                input_value=value,
            )
            result = await choose_auto_completion_dropdown(
                context=input_or_select_context,
                page=page,
                dom=dom,
                text=value,
                preserved_elements=result.incremental_elements,
                skyvern_element=skyvern_element,
                step=step,
                task=task,
            )
            if isinstance(result.action_result, ActionSuccess):
                return ActionSuccess()

            tried_values.append(value)
            whole_new_elements.extend(result.incremental_elements)

        # WARN: currently, we don't trigger this logic because MAX_AUTO_COMPLETE_ATTEMP is 1, to speed up skyvern
        if current_attemp < MAX_AUTO_COMPLETE_ATTEMP:
            LOG.info(
                "Ask LLM to tweak the current value based on tried input values",
                step_id=step.step_id,
                task_id=task.task_id,
                current_value=current_value,
                current_attemp=current_attemp,
            )
            cleaned_new_elements = remove_duplicated_HTML_element(whole_new_elements)
            prompt = prompt_engine.load_prompt(
                "auto-completion-tweak-value",
                field_information=field_information,
                current_value=current_value,
                navigation_goal=task.navigation_goal,
                navigation_payload_str=json.dumps(task.navigation_payload),
                tried_values=json.dumps(tried_values),
                popped_up_elements="".join([json_to_html(element) for element in cleaned_new_elements]),
                local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
            )
            json_respone = await app.SECONDARY_LLM_API_HANDLER(
                prompt=prompt, step=step, prompt_name="auto-completion-tweak-value"
            )
            context_reasoning = json_respone.get("reasoning")
            new_current_value = json_respone.get("tweaked_value", "")
            if not new_current_value:
                return ActionFailure(ErrEmptyTweakValue(reasoning=context_reasoning, current_value=current_value))
            LOG.info(
                "Ask LLM tweaked the current value with a new value",
                step_id=step.step_id,
                task_id=task.task_id,
                field_information=input_or_select_context.field,
                current_value=current_value,
                new_value=new_current_value,
            )
            current_value = new_current_value

    else:
        LOG.warning(
            "Auto completion didn't finish, this might leave the input value to be empty.",
            context=input_or_select_context,
            step_id=step.step_id,
            task_id=task.task_id,
        )
        return None


@TraceManager.traced_async(
    ignore_inputs=[
        "input_or_select_context",
        "page",
        "dom",
        "skyvern_element",
        "skyvern_frame",
        "incremental_scraped",
        "dropdown_menu_element",
        "target_value",
        "continue_until_close",
    ]
)
async def sequentially_select_from_dropdown(
    action: SelectOptionAction,
    input_or_select_context: InputOrSelectContext,
    page: Page,
    dom: DomUtil,
    skyvern_element: SkyvernElement,
    skyvern_frame: SkyvernFrame,
    incremental_scraped: IncrementalScrapePage,
    step: Step,
    task: Task,
    dropdown_menu_element: SkyvernElement | None = None,
    force_select: bool = False,
    target_value: str = "",
    continue_until_close: bool = False,
) -> CustomSingleSelectResult | None:
    """
    TODO: support to return all values retrieved from the sequentially select
    Only return the last value today
    """
    if not force_select and input_or_select_context.is_search_bar:
        LOG.info(
            "Exit custom selection mode since it's a non-force search bar",
            context=input_or_select_context,
            task_id=task.task_id,
            step_id=step.step_id,
        )
        return None

    # TODO: only support the third-level dropdown selection now, but for date picker, we need to support more levels as it will move the month, year, etc.
    MAX_DATEPICKER_DEPTH = 30
    MAX_SELECT_DEPTH = 3
    max_depth = MAX_DATEPICKER_DEPTH if input_or_select_context.is_date_related else MAX_SELECT_DEPTH
    values: list[str | None] = []
    select_history: list[CustomSingleSelectResult] = []
    single_select_result: CustomSingleSelectResult | None = None

    check_filter_funcs: list[CheckFilterOutElementIDFunc] = [check_existed_but_not_option_element_in_dom_factory(dom)]
    for i in range(max_depth):
        single_select_result = await select_from_dropdown(
            context=input_or_select_context,
            page=page,
            skyvern_element=skyvern_element,
            skyvern_frame=skyvern_frame,
            incremental_scraped=incremental_scraped,
            check_filter_funcs=check_filter_funcs,
            step=step,
            task=task,
            dropdown_menu_element=dropdown_menu_element,
            select_history=select_history,
            force_select=force_select,
            target_value=target_value,
        )
        assert single_select_result is not None
        select_history.append(single_select_result)
        values.append(single_select_result.value)
        # wait 1s until DOM finished updating
        await asyncio.sleep(1)

        if await single_select_result.is_done():
            return single_select_result

        if i == max_depth - 1:
            LOG.warning(
                "Reaching the max selection depth",
                depth=i,
                task_id=task.task_id,
                step_id=step.step_id,
            )
            break

        LOG.info(
            "Seems to be a multi-level selection, continue to select until it finishes",
            selected_time=i + 1,
            task_id=task.task_id,
            step_id=step.step_id,
        )
        # wait for 3s to load new options
        await asyncio.sleep(3)

        check_filter_funcs.append(
            check_disappeared_element_id_in_incremental_factory(incremental_scraped=incremental_scraped)
        )

        secondary_increment_element = await incremental_scraped.get_incremental_element_tree(
            clean_and_remove_element_tree_factory(
                task=task,
                step=step,
                check_filter_funcs=check_filter_funcs,
            )
        )
        if len(secondary_increment_element) == 0:
            LOG.info(
                "No incremental element detected for the next level selection, going to quit the custom select mode",
                selected_time=i + 1,
                task_id=task.task_id,
                step_id=step.step_id,
            )
            return single_select_result

        # it's for typing. it's been verified in `single_select_result.is_done()`
        assert single_select_result.dropdown_menu is not None

        if single_select_result.action_type is not None and single_select_result.action_type == ActionType.INPUT_TEXT:
            LOG.info(
                "It's an input mini action, going to continue the select action",
                step_id=step.step_id,
                task_id=task.task_id,
            )
            continue

        if continue_until_close:
            LOG.info(
                "Continue the selecting until the dropdown menu is closed",
                step_id=step.step_id,
                task_id=task.task_id,
            )
            continue

        screenshot = await page.screenshot(timeout=settings.BROWSER_SCREENSHOT_TIMEOUT_MS)
        mini_goal = (
            input_or_select_context.field
            if not input_or_select_context.intention
            else input_or_select_context.intention
        )
        prompt = prompt_engine.load_prompt(
            "confirm-multi-selection-finish",
            mini_goal=mini_goal,
            navigation_goal=task.navigation_goal,
            navigation_payload_str=json.dumps(task.navigation_payload),
            elements="".join(json_to_html(element) for element in secondary_increment_element),
            select_history=json.dumps(build_sequential_select_history(select_history)),
            local_datetime=datetime.now(ensure_context().tz_info).isoformat(),
        )
        llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(task.llm_key, default=app.LLM_API_HANDLER)
        json_response = await llm_api_handler(
            prompt=prompt, screenshots=[screenshot], step=step, prompt_name="confirm-multi-selection-finish"
        )
        if json_response.get("is_mini_goal_finished", False):
            LOG.info("The user has finished the selection for the current opened dropdown", step_id=step.step_id)
            return single_select_result
    else:
        if input_or_select_context.is_date_related:
            if skyvern_element.get_tag_name() == InteractiveElement.INPUT and action.option.label:
                try:
                    LOG.info(
                        "Try to input the date directly",
                        step_id=step.step_id,
                        task_id=task.task_id,
                    )
                    await skyvern_element.input_sequentially(action.option.label)
                    result = CustomSingleSelectResult(skyvern_frame=skyvern_frame)
                    result.action_result = ActionSuccess()
                    return result

                except Exception:
                    LOG.warning(
                        "Failed to input the date directly",
                        exc_info=True,
                        step_id=step.step_id,
                        task_id=task.task_id,
                    )

            if single_select_result and single_select_result.action_result:
                single_select_result.action_result.skip_remaining_actions = True
                return single_select_result

    return select_history[-1] if len(select_history) > 0 else None


def build_sequential_select_history(history_list: list[CustomSingleSelectResult]) -> list[dict[str, Any]]:
    result = [
        {
            "reasoning": select_result.reasoning,
            "value": select_result.value,
            "result": "success" if isinstance(select_result.action_result, ActionSuccess) else "failed",
        }
        for select_result in history_list
    ]
    return result


class CustomSelectPromptOptions(BaseModel):
    """
    This is the options for the custom select prompt.
    It's used to generate the prompt for the custom select action.
    is_date_related: whether the field is date related
    required_field: whether the field is required
    field_information: the description about the field, could be field name, action intention, action reasoning about the field, etc.
    target_value: the target value of the field (generated by the LLM in the main prompt).
    """

    is_date_related: bool = False
    required_field: bool = False
    field_information: str = ""
    target_value: str | None = None


@TraceManager.traced_async(ignore_inputs=["scraped_page", "page"])
async def select_from_emerging_elements(
    current_element_id: str,
    options: CustomSelectPromptOptions,
    page: Page,
    scraped_page: ScrapedPage,
    step: Step,
    task: Task,
    support_complete_action: bool = False,
) -> ActionResult:
    """
    This is the function to select an element from the new showing elements.
    Currently mainly used for the dropdown menu selection.
    """

    # TODO: support to handle the case when options are loaded by scroll
    scraped_page_after_open = await scraped_page.generate_scraped_page_without_screenshots()
    new_element_ids = set(scraped_page_after_open.id_to_css_dict.keys()) - set(scraped_page.id_to_css_dict.keys())

    dom_after_open = DomUtil(scraped_page=scraped_page_after_open, page=page)
    new_interactable_element_ids = [
        element_id
        for element_id in new_element_ids
        if (await dom_after_open.get_skyvern_element_by_id(element_id)).is_interactable()
    ]

    if len(new_interactable_element_ids) == 0:
        raise NoIncrementalElementFoundForCustomSelection(element_id=current_element_id)

    prompt = load_prompt_with_elements(
        element_tree_builder=scraped_page_after_open,
        prompt_engine=prompt_engine,
        template_name="custom-select",
        is_date_related=options.is_date_related,
        field_information=options.field_information,
        required_field=options.required_field,
        target_value=options.target_value,
        navigation_goal=task.navigation_goal,
        new_elements_ids=new_interactable_element_ids,
        support_complete_action=support_complete_action,
        navigation_payload_str=json.dumps(task.navigation_payload),
        local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
    )
    LOG.info(
        "Calling LLM to find the match element",
        step_id=step.step_id,
        task_id=task.task_id,
    )

    llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(task.llm_key, default=app.LLM_API_HANDLER)
    json_response = await llm_api_handler(prompt=prompt, step=step, prompt_name="custom-select")
    value: str | None = json_response.get("value", None)
    LOG.info(
        "LLM response for the matched element",
        matched_value=value,
        response=json_response,
        step_id=step.step_id,
        task_id=task.task_id,
    )

    action_type_str: str = json_response.get("action_type", "") or ""
    action_type = ActionType(action_type_str.lower())
    element_id: str | None = json_response.get("id", None)
    if not element_id or action_type not in [ActionType.CLICK, ActionType.INPUT_TEXT, ActionType.COMPLETE]:
        raise NoAvailableOptionFoundForCustomSelection(reason=json_response.get("reasoning"))

    if action_type == ActionType.COMPLETE:
        LOG.info(
            "The user has completed the user goal in the current opened dropdown, although the dropdown might not be closed",
            step_id=step.step_id,
            task_id=task.task_id,
        )
        return ActionSuccess()

    if value is not None and action_type == ActionType.INPUT_TEXT:
        LOG.info(
            "No clickable option found, but found input element to search",
            element_id=element_id,
        )
        input_element = await dom_after_open.get_skyvern_element_by_id(element_id)
        await input_element.scroll_into_view()
        current_text = await get_input_value(input_element.get_tag_name(), input_element.get_locator())
        if current_text == value:
            return ActionSuccess()

        await input_element.input_clear()
        await input_element.input_sequentially(value)
        return ActionSuccess()

    else:
        selected_element = await dom_after_open.get_skyvern_element_by_id(element_id)
        if await selected_element.get_attr("role") == "listbox":
            return ActionFailure(exception=InteractWithDropdownContainer(element_id=element_id))

    await selected_element.scroll_into_view()
    await selected_element.click(page=page)
    return ActionSuccess()


@TraceManager.traced_async(
    ignore_inputs=[
        "context",
        "page",
        "skyvern_element",
        "skyvern_frame",
        "incremental_scraped",
        "check_filter_funcs",
        "dropdown_menu_element",
        "select_history",
        "target_value",
    ]
)
async def select_from_dropdown(
    context: InputOrSelectContext,
    page: Page,
    skyvern_element: SkyvernElement,
    skyvern_frame: SkyvernFrame,
    incremental_scraped: IncrementalScrapePage,
    check_filter_funcs: list[CheckFilterOutElementIDFunc],
    step: Step,
    task: Task,
    dropdown_menu_element: SkyvernElement | None = None,
    select_history: list[CustomSingleSelectResult] | None = None,
    force_select: bool = False,
    target_value: str = "",
) -> CustomSingleSelectResult:
    """
    force_select: is used to choose an element to click even there's no dropdown menu;
    targe_value: only valid when force_select is "False". When target_value is not empty, the matched option must be relevant to target value;
    None will be only returned when:
        1. force_select is false and no dropdown menu popped
        2. force_select is false and match value is not relevant to the target value
    """
    select_history = [] if select_history is None else select_history
    single_select_result = CustomSingleSelectResult(skyvern_frame=skyvern_frame)

    timeout = settings.BROWSER_ACTION_TIMEOUT_MS

    if dropdown_menu_element is None:
        dropdown_menu_element = await locate_dropdown_menu(
            current_anchor_element=skyvern_element,
            incremental_scraped=incremental_scraped,
            step=step,
            task=task,
        )
    single_select_result.dropdown_menu = dropdown_menu_element

    if not force_select and dropdown_menu_element is None:
        return single_select_result

    if dropdown_menu_element:
        potential_scrollable_element = await try_to_find_potential_scrollable_element(
            skyvern_element=dropdown_menu_element,
            incremental_scraped=incremental_scraped,
            step=step,
            task=task,
        )

        if await skyvern_frame.get_element_scrollable(await potential_scrollable_element.get_element_handler()):
            await scroll_down_to_load_all_options(
                scrollable_element=potential_scrollable_element,
                skyvern_frame=skyvern_frame,
                page=page,
                incremental_scraped=incremental_scraped,
                step=step,
                task=task,
            )

    trimmed_element_tree = await incremental_scraped.get_incremental_element_tree(
        clean_and_remove_element_tree_factory(task=task, step=step, check_filter_funcs=check_filter_funcs),
    )
    incremental_scraped.set_element_tree_trimmed(trimmed_element_tree)
    html = incremental_scraped.build_element_tree(html_need_skyvern_attrs=True)

    skyvern_context = ensure_context()
    prompt = prompt_engine.load_prompt(
        "custom-select",
        is_date_related=context.is_date_related,
        field_information=context.field if not context.intention else context.intention,
        required_field=context.is_required,
        target_value=target_value,
        navigation_goal=task.navigation_goal,
        navigation_payload_str=json.dumps(task.navigation_payload),
        elements=html,
        select_history=json.dumps(build_sequential_select_history(select_history)) if select_history else "",
        local_datetime=datetime.now(skyvern_context.tz_info).isoformat(),
    )

    LOG.info(
        "Calling LLM to find the match element",
        step_id=step.step_id,
        task_id=task.task_id,
    )
    json_response = await app.SELECT_AGENT_LLM_API_HANDLER(prompt=prompt, step=step, prompt_name="custom-select")
    value: str | None = json_response.get("value", None)
    single_select_result.value = value
    select_reason: str | None = json_response.get("reasoning", None)
    single_select_result.reasoning = select_reason

    LOG.info(
        "LLM response for the matched element",
        matched_value=value,
        response=json_response,
        step_id=step.step_id,
        task_id=task.task_id,
    )

    action_type: str = json_response.get("action_type", "") or ""
    action_type = action_type.lower()
    single_select_result.action_type = ActionType(action_type)
    element_id: str | None = json_response.get("id", None)
    if not element_id or action_type not in [ActionType.CLICK, ActionType.INPUT_TEXT]:
        raise NoAvailableOptionFoundForCustomSelection(reason=json_response.get("reasoning"))

    if not force_select and target_value:
        if not json_response.get("relevant", False):
            LOG.info(
                "The selected option is not relevant to the target value",
                element_id=element_id,
                task_id=task.task_id,
                step_id=step.step_id,
            )
            return single_select_result

    if value is not None and action_type == ActionType.INPUT_TEXT:
        LOG.info(
            "No clickable option found, but found input element to search",
            element_id=element_id,
            task_id=task.task_id,
            step_id=step.step_id,
        )
        try:
            input_element = await SkyvernElement.create_from_incremental(incremental_scraped, element_id)
            await input_element.scroll_into_view()
            current_text = await get_input_value(input_element.get_tag_name(), input_element.get_locator())
            if current_text == value:
                single_select_result.action_result = ActionSuccess()
                return single_select_result

            await input_element.input_clear()
            await input_element.input_sequentially(value)
            single_select_result.action_result = ActionSuccess()
            return single_select_result
        except Exception as e:
            single_select_result.action_result = ActionFailure(exception=e)
            return single_select_result

    try:
        selected_element = await SkyvernElement.create_from_incremental(incremental_scraped, element_id)
        # TODO Some popup dropdowns include <select> element, we only handle the <select> element now, to prevent infinite recursion. Need to support more types of dropdowns.
        if selected_element.get_tag_name() == InteractiveElement.SELECT and value:
            await selected_element.scroll_into_view()
            action = SelectOptionAction(
                reasoning=select_reason,
                element_id=element_id,
                option=SelectOption(label=value),
            )
            results = await normal_select(
                action=action, skyvern_element=selected_element, task=task, step=step, builder=incremental_scraped
            )
            assert len(results) > 0
            single_select_result.action_result = results[0]
            return single_select_result

        if await selected_element.get_attr("role") == "listbox":
            single_select_result.action_result = ActionFailure(
                exception=InteractWithDropdownContainer(element_id=element_id)
            )
            return single_select_result

        await selected_element.scroll_into_view()
        await selected_element.click(page=page, timeout=timeout)
        single_select_result.action_result = ActionSuccess()
        return single_select_result
    except (MissingElement, MissingElementDict, MissingElementInCSSMap, MultipleElementsFound):
        if not value:
            raise

    # sometimes we have multiple elements pointed to the same value,
    # but only one option is clickable on the page
    LOG.debug(
        "Searching option with the same value in incremental elements",
        value=value,
        elements=incremental_scraped.element_tree,
    )
    locator = await incremental_scraped.select_one_element_by_value(value=value)
    if not locator:
        single_select_result.action_result = ActionFailure(exception=MissingElement())
        return single_select_result

    try:
        LOG.info(
            "Find an alternative option with the same value. Try to select the option.",
            value=value,
        )
        await locator.click(timeout=timeout)
        single_select_result.action_result = ActionSuccess()
        return single_select_result
    except Exception as e:
        single_select_result.action_result = ActionFailure(exception=e)
        return single_select_result


@TraceManager.traced_async(
    ignore_inputs=[
        "value",
        "page",
        "skyvern_element",
        "skyvern_frame",
        "dom",
        "incremental_scraped",
        "dropdown_menu_element",
    ]
)
async def select_from_dropdown_by_value(
    value: str,
    page: Page,
    skyvern_element: SkyvernElement,
    skyvern_frame: SkyvernFrame,
    dom: DomUtil,
    incremental_scraped: IncrementalScrapePage,
    task: Task,
    step: Step,
    dropdown_menu_element: SkyvernElement | None = None,
) -> ActionResult:
    timeout = settings.BROWSER_ACTION_TIMEOUT_MS
    await incremental_scraped.get_incremental_element_tree(
        clean_and_remove_element_tree_factory(
            task=task, step=step, check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)]
        ),
    )

    element_locator = await incremental_scraped.select_one_element_by_value(value=value)
    if element_locator is not None:
        await element_locator.click(timeout=timeout)
        return ActionSuccess()

    if dropdown_menu_element is None:
        dropdown_menu_element = await locate_dropdown_menu(
            current_anchor_element=skyvern_element,
            incremental_scraped=incremental_scraped,
            step=step,
            task=task,
        )

    if not dropdown_menu_element:
        raise NoElementMatchedForTargetOption(target=value, reason="No value matched")

    potential_scrollable_element = await try_to_find_potential_scrollable_element(
        skyvern_element=dropdown_menu_element,
        incremental_scraped=incremental_scraped,
        task=task,
        step=step,
    )
    if not await skyvern_frame.get_element_scrollable(await potential_scrollable_element.get_element_handler()):
        raise NoElementMatchedForTargetOption(
            target=value, reason="No value matched and element can't scroll to find more options"
        )

    selected: bool = False

    async def continue_callback(incre_scraped: IncrementalScrapePage) -> bool:
        await incre_scraped.get_incremental_element_tree(
            clean_and_remove_element_tree_factory(
                task=task, step=step, check_filter_funcs=[check_existed_but_not_option_element_in_dom_factory(dom)]
            ),
        )

        element_locator = await incre_scraped.select_one_element_by_value(value=value)
        if element_locator is not None:
            await element_locator.click(timeout=timeout)
            nonlocal selected
            selected = True
            return False

        return True

    await scroll_down_to_load_all_options(
        scrollable_element=potential_scrollable_element,
        page=page,
        skyvern_frame=skyvern_frame,
        incremental_scraped=incremental_scraped,
        step=step,
        task=task,
        page_by_page=True,
        is_continue=continue_callback,
    )

    if selected:
        return ActionSuccess()

    raise NoElementMatchedForTargetOption(target=value, reason="No value matched after scrolling")


async def locate_dropdown_menu(
    current_anchor_element: SkyvernElement,
    incremental_scraped: IncrementalScrapePage,
    step: Step,
    task: Task,
) -> SkyvernElement | None:
    # the anchor must exist in the DOM, but no need to be visible css style
    if not await current_anchor_element.is_visible(must_visible_style=False):
        return None

    skyvern_frame = incremental_scraped.skyvern_frame

    for idx, element_dict in enumerate(incremental_scraped.element_tree):
        # FIXME: confirm max to 10 nodes for now, preventing sendindg too many requests to LLM
        if idx >= 10:
            break

        element_id = element_dict.get("id")
        if not element_id:
            LOG.debug(
                "Skip the element without id for the dropdown menu confirm",
                step_id=step.step_id,
                task_id=task.task_id,
                element=element_dict,
            )
            continue

        try:
            head_element = await SkyvernElement.create_from_incremental(incremental_scraped, element_id)
        except Exception:
            LOG.debug(
                "Failed to get head element in the incremental page",
                element_id=element_id,
                step_id=step.step_id,
                task_id=task.task_id,
                exc_info=True,
            )
            continue

        try:
            if not await head_element.is_next_to_element(
                target_locator=current_anchor_element.get_locator(),
                max_x_distance=DROPDOWN_MENU_MAX_DISTANCE,
                max_y_distance=DROPDOWN_MENU_MAX_DISTANCE,
            ):
                LOG.debug(
                    "Skip the element since it's too far away from the anchor element",
                    step_id=step.step_id,
                    task_id=task.task_id,
                    element_id=element_id,
                )
                continue

        except Exception:
            LOG.info(
                "Failed to calculate the distance between the elements",
                element_id=element_id,
                step_id=step.step_id,
                task_id=task.task_id,
                exc_info=True,
            )
            continue

        if not await skyvern_frame.get_element_visible(await head_element.get_element_handler()):
            LOG.debug(
                "Skip the element since it's invisible",
                step_id=step.step_id,
                task_id=task.task_id,
                element_id=element_id,
            )
            continue

        ul_or_listbox_element_id = await head_element.find_children_element_id_by_callback(
            cb=is_ul_or_listbox_element_factory(incremental_scraped=incremental_scraped, task=task, step=step),
        )

        if ul_or_listbox_element_id:
            try:
                await SkyvernElement.create_from_incremental(incremental_scraped, ul_or_listbox_element_id)
                LOG.info(
                    "Confirm it's an opened dropdown menu since it includes <ul> or <role='listbox'>",
                    step_id=step.step_id,
                    task_id=task.task_id,
                    element_id=element_id,
                )
                return await SkyvernElement.create_from_incremental(
                    incre_page=incremental_scraped, element_id=element_id
                )
            except Exception:
                LOG.debug(
                    "Failed to get <ul> or <role='listbox'> element in the incremental page",
                    element_id=element_id,
                    step_id=step.step_id,
                    task_id=task.task_id,
                    exc_info=True,
                )

        # sometimes taking screenshot might scroll away, need to scroll back after the screenshot
        x, y = await skyvern_frame.get_scroll_x_y()
        screenshot = await head_element.get_locator().screenshot(timeout=settings.BROWSER_SCREENSHOT_TIMEOUT_MS)
        await skyvern_frame.scroll_to_x_y(x, y)

        # TODO: better to send untrimmed HTML without skyvern attributes in the future
        dropdown_confirm_prompt = prompt_engine.load_prompt("opened-dropdown-confirm")
        LOG.debug(
            "Confirm if it's an opened dropdown menu",
            step_id=step.step_id,
            task_id=task.task_id,
            element=element_dict,
        )
        json_response = await app.SECONDARY_LLM_API_HANDLER(
            prompt=dropdown_confirm_prompt, screenshots=[screenshot], step=step, prompt_name="opened-dropdown-confirm"
        )
        is_opened_dropdown_menu = json_response.get("is_opened_dropdown_menu")
        if is_opened_dropdown_menu:
            LOG.info(
                "Opened dropdown menu found",
                step_id=step.step_id,
                task_id=task.task_id,
                element_id=element_id,
            )
            return await SkyvernElement.create_from_incremental(incre_page=incremental_scraped, element_id=element_id)
    return None


async def try_to_find_potential_scrollable_element(
    skyvern_element: SkyvernElement,
    incremental_scraped: IncrementalScrapePage,
    task: Task,
    step: Step,
) -> SkyvernElement:
    """
    check any <ul> or <role="listbox"> element in the chidlren.
    if yes, return the found element,
    else, return the orginal one
    """
    found_element_id = await skyvern_element.find_children_element_id_by_callback(
        cb=is_ul_or_listbox_element_factory(incremental_scraped=incremental_scraped, task=task, step=step),
    )
    if found_element_id and found_element_id != skyvern_element.get_id():
        LOG.debug(
            "Found 'ul or listbox' element in children list",
            element_id=found_element_id,
            step_id=step.step_id,
            task_id=task.task_id,
        )

        try:
            skyvern_element = await SkyvernElement.create_from_incremental(incremental_scraped, found_element_id)
        except Exception:
            LOG.debug(
                "Failed to get head element by found element id, use the original element id",
                element_id=found_element_id,
                step_id=step.step_id,
                task_id=task.task_id,
                exc_info=True,
            )
    return skyvern_element


@TraceManager.traced_async(
    ignore_inputs=["scrollable_element", "page", "skyvern_frame", "incremental_scraped", "is_continue"]
)
async def scroll_down_to_load_all_options(
    scrollable_element: SkyvernElement,
    page: Page,
    skyvern_frame: SkyvernFrame,
    incremental_scraped: IncrementalScrapePage,
    step: Step | None = None,
    task: Task | None = None,
    page_by_page: bool = False,
    is_continue: Callable[[IncrementalScrapePage], Awaitable[bool]] | None = None,
) -> None:
    LOG.info(
        "Scroll down the dropdown menu to load all options",
        step_id=step.step_id if step else "none",
        task_id=task.task_id if task else "none",
    )
    timeout = settings.BROWSER_ACTION_TIMEOUT_MS

    dropdown_menu_element_handle = await scrollable_element.get_locator().element_handle(timeout=timeout)
    if dropdown_menu_element_handle is None:
        LOG.info("element handle is None, using focus to move the cursor", element_id=scrollable_element.get_id())
        await scrollable_element.get_locator().focus(timeout=timeout)
    else:
        await dropdown_menu_element_handle.scroll_into_view_if_needed(timeout=timeout)

    await scrollable_element.move_mouse_to_safe(page=page)

    scroll_pace = 0
    previous_num = await incremental_scraped.get_incremental_elements_num()

    deadline = datetime.now(timezone.utc) + timedelta(milliseconds=settings.OPTION_LOADING_TIMEOUT_MS)
    while datetime.now(timezone.utc) < deadline:
        # make sure we can scroll to the bottom
        scroll_interval = settings.BROWSER_HEIGHT * 5
        if dropdown_menu_element_handle is None:
            LOG.info("element handle is None, using mouse to scroll down", element_id=scrollable_element.get_id())
            await page.mouse.wheel(0, scroll_interval)
            scroll_pace += scroll_interval
        else:
            await skyvern_frame.scroll_to_element_bottom(dropdown_menu_element_handle, page_by_page)
            # wait until animation ends, otherwise the scroll operation could be overwritten
            await asyncio.sleep(2)

        # scroll a little back and scroll down to trigger the loading
        await page.mouse.wheel(0, -1e-5)
        await page.mouse.wheel(0, 1e-5)
        # wait for while to load new options
        await asyncio.sleep(10)

        current_num = await incremental_scraped.get_incremental_elements_num()
        LOG.info(
            "Current incremental elements count during the scrolling",
            num=current_num,
            step_id=step.step_id if step else "none",
            task_id=task.task_id if task else "none",
        )

        if is_continue is not None and not await is_continue(incremental_scraped):
            return

        if previous_num == current_num:
            break
        previous_num = current_num
    else:
        LOG.warning("Timeout to load all options, maybe some options will be missed")

    # scroll back to the start point and wait for a while to make all options invisible on the page
    if dropdown_menu_element_handle is None:
        LOG.info("element handle is None, using mouse to scroll back", element_id=scrollable_element.get_id())
        await page.mouse.wheel(0, -scroll_pace)
    else:
        await skyvern_frame.scroll_to_element_top(dropdown_menu_element_handle)
    await asyncio.sleep(5)


async def normal_select(
    action: actions.SelectOptionAction,
    skyvern_element: SkyvernElement,
    task: Task,
    step: Step,
    builder: ElementTreeBuilder,
) -> List[ActionResult]:
    try:
        current_text = await skyvern_element.get_attr("selected")
        if current_text and (current_text == action.option.label or current_text == action.option.value):
            return [ActionSuccess()]
    except Exception:
        LOG.info("failed to confirm if the select option has been done, force to take the action again.")

    action_result: List[ActionResult] = []
    is_success = False
    locator = skyvern_element.get_locator()

    prompt = load_prompt_with_elements(
        element_tree_builder=builder,
        prompt_engine=prompt_engine,
        template_name="parse-input-or-select-context",
        action_reasoning=action.reasoning,
        element_id=action.element_id,
    )
    json_response = await app.SECONDARY_LLM_API_HANDLER(
        prompt=prompt, step=step, prompt_name="parse-input-or-select-context"
    )
    json_response["intention"] = action.intention
    input_or_select_context = InputOrSelectContext.model_validate(json_response)
    LOG.info(
        "Parsed input/select context",
        context=input_or_select_context,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    await skyvern_element.refresh_select_options()
    options_html = skyvern_element.build_HTML()
    field_information = (
        input_or_select_context.field if not input_or_select_context.intention else input_or_select_context.intention
    )
    prompt = prompt_engine.load_prompt(
        "normal-select",
        field_information=field_information,
        required_field=input_or_select_context.is_required,
        navigation_goal=task.navigation_goal,
        navigation_payload_str=json.dumps(task.navigation_payload),
        options=options_html,
        local_datetime=datetime.now(skyvern_context.ensure_context().tz_info).isoformat(),
    )

    json_response = await app.SELECT_AGENT_LLM_API_HANDLER(prompt=prompt, step=step, prompt_name="custom-select")
    index: int | None = json_response.get("index")
    value: str | None = json_response.get("value")

    try:
        await locator.click(
            timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
        )
    except Exception as e:
        LOG.info(
            "Failed to click before select action",
            exc_info=True,
            action=action,
            locator=locator,
        )
        action_result.append(ActionFailure(e))
        return action_result

    if not is_success and value is not None:
        try:
            # click by value (if it matches)
            await locator.select_option(
                value=value,
                timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
            )
            is_success = True
            action_result.append(ActionSuccess())
        except Exception:
            action_result.append(ActionFailure(FailToSelectByValue(action.element_id)))
            LOG.info(
                "Failed to take select action by value",
                exc_info=True,
                action=action,
                locator=locator,
            )

    if not is_success and index is not None:
        if index >= len(skyvern_element.get_options()):
            action_result.append(ActionFailure(OptionIndexOutOfBound(action.element_id)))
            LOG.info(
                "option index is out of bound",
                action=action,
                locator=locator,
            )
        else:
            try:
                # This means the supplied index was for the select element, not a reference to the css dict
                await locator.select_option(
                    index=index,
                    timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
                )
                is_success = True
                action_result.append(ActionSuccess())
            except Exception:
                action_result.append(ActionFailure(FailToSelectByIndex(action.element_id)))
                LOG.info(
                    "Failed to click on the option by index",
                    exc_info=True,
                    action=action,
                    locator=locator,
                )

    try:
        await locator.click(
            timeout=settings.BROWSER_ACTION_TIMEOUT_MS,
        )
    except Exception as e:
        LOG.info(
            "Failed to click after select action",
            exc_info=True,
            action=action,
            locator=locator,
        )
        action_result.append(ActionFailure(e))
        return action_result

    if len(action_result) == 0:
        action_result.append(ActionFailure(EmptySelect(element_id=action.element_id)))

    return action_result


def get_anchor_to_click(scraped_page: ScrapedPage, element_id: str) -> str | None:
    """
    Get the anchor tag under the label to click
    """
    LOG.info("Getting anchor tag to click", element_id=element_id)
    for ele in scraped_page.elements:
        if "id" in ele and ele["id"] == element_id:
            for child in ele["children"]:
                if "tagName" in child and child["tagName"] == "a":
                    return scraped_page.id_to_css_dict[child["id"]]
    return None


def get_select_id_in_label_children(scraped_page: ScrapedPage, element_id: str) -> str | None:
    """
    search <select> in the children of <label>
    """
    LOG.info("Searching select in the label children", element_id=element_id)
    element = scraped_page.id_to_element_dict.get(element_id, None)
    if element is None:
        return None

    for child in element.get("children", []):
        if child.get("tagName", "") == "select":
            return child.get("id", None)

    return None


def get_checkbox_id_in_label_children(scraped_page: ScrapedPage, element_id: str) -> str | None:
    """
    search checkbox/radio in the children of <label>
    """
    LOG.info("Searching checkbox/radio in the label children", element_id=element_id)
    element = scraped_page.id_to_element_dict.get(element_id, None)
    if element is None:
        return None

    for child in element.get("children", []):
        if child.get("tagName", "") == "input" and child.get("attributes", {}).get("type") in ["checkbox", "radio"]:
            return child.get("id", None)

    return None


async def extract_information_for_navigation_goal(
    task: Task,
    step: Step,
    scraped_page: ScrapedPage,
) -> ScrapeResult:
    """
    Scrapes a webpage and returns the scraped response, including:
    1. JSON representation of what the user is seeing
    2. The scraped page
    """
    scraped_page_refreshed = await scraped_page.refresh()
    context = ensure_context()
    extract_information_prompt = load_prompt_with_elements(
        element_tree_builder=scraped_page_refreshed,
        prompt_engine=prompt_engine,
        template_name="extract-information",
        html_need_skyvern_attrs=False,
        navigation_goal=task.navigation_goal,
        navigation_payload=task.navigation_payload,
        previous_extracted_information=task.extracted_information,
        data_extraction_goal=task.data_extraction_goal,
        extracted_information_schema=task.extracted_information_schema,
        current_url=scraped_page_refreshed.url,
        extracted_text=scraped_page_refreshed.extracted_text,
        error_code_mapping_str=(json.dumps(task.error_code_mapping) if task.error_code_mapping else None),
        local_datetime=datetime.now(context.tz_info).isoformat(),
    )

    llm_key_override = task.llm_key
    if await is_cua_task(task=task):
        # CUA tasks should use the default data extraction llm key
        llm_key_override = None

    llm_api_handler = LLMAPIHandlerFactory.get_override_llm_api_handler(llm_key_override, default=app.LLM_API_HANDLER)
    json_response = await llm_api_handler(
        prompt=extract_information_prompt,
        step=step,
        screenshots=scraped_page.screenshots,
        prompt_name="extract-information",
    )

    return ScrapeResult(
        scraped_data=json_response,
    )


async def click_listbox_option(
    scraped_page: ScrapedPage,
    page: Page,
    action: actions.SelectOptionAction,
    listbox_element_id: str,
) -> bool:
    listbox_element = scraped_page.id_to_element_dict.get(listbox_element_id)
    if listbox_element is None:
        return False
    # this is a listbox element, get all the children
    if "children" not in listbox_element:
        return False

    LOG.info("starting bfs", listbox_element_id=listbox_element_id)
    bfs_queue = [child for child in listbox_element["children"]]
    while bfs_queue:
        child = bfs_queue.pop(0)
        LOG.info("popped child", element_id=child["id"])
        if "attributes" in child and "role" in child["attributes"] and child["attributes"]["role"] == "option":
            LOG.info("found option", element_id=child["id"])
            text = child["text"] if "text" in child else ""
            if text and (text == action.option.label or text == action.option.value):
                dom = DomUtil(scraped_page=scraped_page, page=page)
                try:
                    skyvern_element = await dom.get_skyvern_element_by_id(child["id"])
                    locator = skyvern_element.locator
                    await locator.click(timeout=1000)

                    return True
                except Exception:
                    LOG.error(
                        "Failed to click on the option",
                        action=action,
                        exc_info=True,
                    )
        if "children" in child:
            bfs_queue.extend(child["children"])
    return False


async def get_input_value(tag_name: str, locator: Locator) -> str | None:
    if tag_name in COMMON_INPUT_TAGS:
        return await locator.input_value()
    # for span, div, p or other tags:
    return await locator.inner_text()


async def poll_verification_code(
    task_id: str,
    organization_id: str,
    workflow_id: str | None = None,
    workflow_run_id: str | None = None,
    workflow_permanent_id: str | None = None,
    totp_verification_url: str | None = None,
    totp_identifier: str | None = None,
) -> str | None:
    timeout = timedelta(minutes=settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS)
    start_datetime = datetime.utcnow()
    timeout_datetime = start_datetime + timeout
    org_token = await app.DATABASE.get_valid_org_auth_token(organization_id, OrganizationAuthTokenType.api)
    if not org_token:
        LOG.error("Failed to get organization token when trying to get verification code")
        return None
    # wait for 40 seconds to let the verification code comes in before polling
    await asyncio.sleep(settings.VERIFICATION_CODE_INITIAL_WAIT_TIME_SECS)
    while True:
        # check timeout
        if datetime.utcnow() > timeout_datetime:
            LOG.warning("Polling verification code timed out")
            raise NoTOTPVerificationCodeFound(
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                workflow_id=workflow_permanent_id,
                totp_verification_url=totp_verification_url,
                totp_identifier=totp_identifier,
            )
        verification_code = None
        if totp_verification_url:
            verification_code = await _get_verification_code_from_url(
                task_id,
                totp_verification_url,
                org_token.token,
                workflow_run_id=workflow_run_id,
            )
        elif totp_identifier:
            verification_code = await _get_verification_code_from_db(
                task_id,
                organization_id,
                totp_identifier,
                workflow_id=workflow_permanent_id,
                workflow_run_id=workflow_run_id,
            )
        if verification_code:
            LOG.info("Got verification code", verification_code=verification_code)
            return verification_code

        await asyncio.sleep(10)


async def _get_verification_code_from_url(
    task_id: str,
    url: str,
    api_key: str,
    workflow_run_id: str | None = None,
    workflow_permanent_id: str | None = None,
) -> str | None:
    request_data = {"task_id": task_id}
    if workflow_run_id:
        request_data["workflow_run_id"] = workflow_run_id
    if workflow_permanent_id:
        request_data["workflow_permanent_id"] = workflow_permanent_id
    payload = json.dumps(request_data)
    signature = generate_skyvern_signature(
        payload=payload,
        api_key=api_key,
    )
    timestamp = str(int(datetime.utcnow().timestamp()))
    headers = {
        "x-skyvern-timestamp": timestamp,
        "x-skyvern-signature": signature,
        "Content-Type": "application/json",
    }
    json_resp = await aiohttp_post(url=url, data=request_data, headers=headers, raise_exception=False)
    return json_resp.get("verification_code", None)


async def _get_verification_code_from_db(
    task_id: str,
    organization_id: str,
    totp_identifier: str,
    workflow_id: str | None = None,
    workflow_run_id: str | None = None,
) -> str | None:
    totp_codes = await app.DATABASE.get_totp_codes(organization_id=organization_id, totp_identifier=totp_identifier)
    for totp_code in totp_codes:
        if totp_code.workflow_run_id and workflow_run_id and totp_code.workflow_run_id != workflow_run_id:
            continue
        if totp_code.workflow_id and workflow_id and totp_code.workflow_id != workflow_id:
            continue
        if totp_code.task_id and totp_code.task_id != task_id:
            continue
        if totp_code.expired_at and totp_code.expired_at < datetime.utcnow():
            continue
        return totp_code.code
    return None


class AbstractActionForContextParse(BaseModel):
    reasoning: str | None
    element_id: str
    intention: str | None


async def _get_input_or_select_context(
    action: InputTextAction | SelectOptionAction | AbstractActionForContextParse, scraped_page: ScrapedPage, step: Step
) -> InputOrSelectContext:
    prompt = load_prompt_with_elements(
        element_tree_builder=scraped_page,
        prompt_engine=prompt_engine,
        template_name="parse-input-or-select-context",
        action_reasoning=action.reasoning,
        element_id=action.element_id,
    )
    json_response = await app.SECONDARY_LLM_API_HANDLER(
        prompt=prompt, step=step, prompt_name="parse-input-or-select-context"
    )
    json_response["intention"] = action.intention
    input_or_select_context = InputOrSelectContext.model_validate(json_response)
    LOG.info(
        "Parsed input/select context",
        context=input_or_select_context,
    )
    return input_or_select_context
