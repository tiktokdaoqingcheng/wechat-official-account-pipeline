from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


EDITORIAL_CONTRACT_VERSION = "2026-08-07.v1"

AI_COPY_PHRASES = (
    "普通读者不用急着判断",
    "读这类消息时",
    "读这条不用先追术语",
    "先看落地位置",
    "先把它当作一条观察线索",
    "先看它有没有真实用户",
    "它值得放进早报",
    "这条消息值得放进今天的科技早报",
    "成为今天科技早报的重点动态",
    "值得关注，因为",
    "换个角度看",
    "后续看",
    "后续看点",
    "后续观察",
    "这条线索可以放在产业落地里看",
    "后面需要继续看客户、交付时间、量产计划和供应链配套",
    "从概念走向",
    "谁使用、谁付费、谁复核",
    "谁会用、怎么付费、结果由谁复核",
    "点击查看原文",
    "欢迎关注",
    "微信公众号",
    "微信号",
    "更多精彩内容",
)

MODEL_BANNED_PHRASES = AI_COPY_PHRASES + (
    "它反映了 AI 正在进入更具体的产品、行业和工作流程",
    "AI 正在进入更具体的产品、行业和工作流程",
    "可以关注",
    "后续可以关注",
    "更值得关注的是",
    "这条消息的重点不是",
    "关键不是又多一个按钮",
)

GENERIC_EDITORIAL_PATTERNS = (
    r"标志着.{0,24}(?:进一步|新阶段|重要突破)",
    r"展现了.{0,24}(?:技术领先|领先性|综合实力)",
    r"引发了(?:市场|行业|外界)对",
    r"加速.{0,16}(?:商业化|产业化|落地)进程",
    r"获得更明确的(?:参考|方向|预期|基准)",
    r"(?:这一)?里程碑事件",
    r"具有显著差异性",
    r"整体发展效率",
    r"为.{0,24}提供了(?:一个)?(?:清晰|明确).{0,12}(?:参考|基准)",
)


@dataclass(frozen=True)
class EditorialContract:
    profile: str
    column_name: str
    article_title_min_elements: int
    article_title_max_elements: int
    article_title_max_chars: int
    detail_heading_min_chars: int
    detail_heading_max_chars: int
    reader_summary_target_min_chars: int
    reader_summary_target_max_chars: int
    reader_summary_accept_min_chars: int
    reader_summary_two_sentence_min_chars: int
    reader_summary_min_sentences: int
    reader_summary_preferred_min_sentences: int
    reader_summary_preferred_max_sentences: int
    reader_summary_output_max_chars: int
    minimum_supported_fact_units: int
    sentence_jobs: tuple[str, ...]
    domain_rules: tuple[str, ...]
    positive_example_heading: str
    positive_example_summary: str


_COMMON = {
    "article_title_min_elements": 2,
    "article_title_max_elements": 4,
    "article_title_max_chars": 64,
    "detail_heading_min_chars": 18,
    "detail_heading_max_chars": 52,
    "reader_summary_target_min_chars": 160,
    "reader_summary_target_max_chars": 230,
    "reader_summary_accept_min_chars": 125,
    "reader_summary_two_sentence_min_chars": 135,
    "reader_summary_min_sentences": 2,
    "reader_summary_preferred_min_sentences": 3,
    "reader_summary_preferred_max_sentences": 4,
    "reader_summary_output_max_chars": 260,
    "minimum_supported_fact_units": 3,
}

TECH_BRIEFING_CONTRACT = EditorialContract(
    profile="tech_briefing",
    column_name="科技早报",
    sentence_jobs=(
        "第1句交代谁在什么范围内做了什么，以及事情当前处于发布、测试还是落地阶段。",
        "第2句补充来源明确给出的数字、产品能力、使用对象或具体场景。",
        "第3句补充开放范围、实施进度、限制条件或来源已经说明的背景。",
        "只有仍有独立事实时才写第4句，不能用评论、预测或读者建议凑长度。",
    ),
    domain_rules=(
        "先写事件，再写细节；不要先发表行业判断。",
        "产品名和英文品牌可以保留，但整句必须是自然中文新闻表达。",
        "影响只有在事实卡明确支持时才能写，不能把编辑推测当成新闻事实。",
    ),
    positive_example_heading="某云服务商开放企业智能助手，首批覆盖文档检索和会议整理",
    positive_example_summary=(
        "首批版本已经进入企业工作区，可在管理员授权后读取内部文档、会议记录和项目资料。"
        "控制台允许按账号限制应用范围，外部发送和批量修改仍需人工确认，调用结果会进入审计记录。"
        "此次开放采用分批方式，个人账户以及尚未接入统一身份系统的组织暂不在支持范围内。"
        "企业可先按部门启用，再根据权限审计、异常调用和人工确认记录决定是否扩大使用范围，试用期间仍由管理员统一调整策略。"
    ),
    **_COMMON,
)

SMART_MANUFACTURING_CONTRACT = EditorialContract(
    profile="smart_manufacturing_daily",
    column_name="智能制造日报",
    sentence_jobs=(
        "第1句交代企业、设备或项目做了什么，并写清产线、工厂、客户或应用场景。",
        "第2句补充来源明确给出的节拍、精度、产能、订单、交付、成本或测试范围。",
        "第3句补充试运行、量产、验收、供应链或人工接管等当前进度和边界。",
        "只有仍有独立事实时才写第4句，不能泛泛讨论智能制造趋势。",
    ),
    domain_rules=(
        "语气采用产业媒体写法，优先写产线、交付、客户、供应链、验证标准和单位经济性。",
        "没有量产、客户或订单证据时，不得把试验、发布或合作意向写成已经商业落地。",
        "技术能力必须落到设备、工序、工厂或交付状态，不能只堆叠AI、机器人和制造概念。",
    ),
    positive_example_heading="某机器人厂商启动汽车装配线试运行，首批设备进入节拍验证",
    positive_example_summary=(
        "首套设备已经进入汽车零部件装配线，测试范围包括抓取精度、连续运行时间和异常停机处理。"
        "现场工程师可在试运行期间调整参数，每次变更都会写入验收记录，并由产线负责人复核。"
        "后续交付将根据节拍和良率结果分批推进，供应商同时保留驻场支持和故障后的人工接管。"
        "项目组将在首轮测试结束后汇总停机次数、参数变更、良率和验收结果，再确认下一批设备的进场时间与部署节奏。"
    ),
    **_COMMON,
)

NEGATIVE_EXAMPLES = (
    {
        "text": "这项更新标志着行业进入新阶段，也展现了企业的技术领先实力，值得持续关注。",
        "problems": ["没有新增事实", "宏大判断", "可套用到任何新闻"],
    },
    {
        "text": "某公司发布了新产品。该产品已经正式发布。此次发布带来了新的产品。",
        "problems": ["同义反复", "信息密度不足", "机械凑句数"],
    },
    {
        "text": "据某媒体报道，该公司发布新品。某媒体消息称，新品已经推出。",
        "problems": ["正文重复来源", "事实重复", "渲染层已经单独显示来源"],
    },
)


def contract_for_article(article: dict[str, Any] | None) -> EditorialContract:
    source_policy = str((article or {}).get("source_policy", "")).strip()
    title = str((article or {}).get("title", "")).strip()
    if source_policy == "smart_manufacturing_daily" or "智能制造日报" in title:
        return SMART_MANUFACTURING_CONTRACT
    return TECH_BRIEFING_CONTRACT


def contract_metadata(article: dict[str, Any] | None) -> dict[str, Any]:
    contract = contract_for_article(article)
    return {
        "version": EDITORIAL_CONTRACT_VERSION,
        "profile": contract.profile,
        "column_name": contract.column_name,
        "article_title_elements": [
            contract.article_title_min_elements,
            contract.article_title_max_elements,
        ],
        "article_title_max_chars": contract.article_title_max_chars,
        "detail_heading_chars": [contract.detail_heading_min_chars, contract.detail_heading_max_chars],
        "reader_summary_target_chars": [
            contract.reader_summary_target_min_chars,
            contract.reader_summary_target_max_chars,
        ],
        "reader_summary_accept_min_chars": contract.reader_summary_accept_min_chars,
        "reader_summary_two_sentence_min_chars": contract.reader_summary_two_sentence_min_chars,
        "reader_summary_preferred_sentences": [
            contract.reader_summary_preferred_min_sentences,
            contract.reader_summary_preferred_max_sentences,
        ],
        "minimum_supported_fact_units": contract.minimum_supported_fact_units,
    }


def fact_extraction_prompt_contract(article: dict[str, Any] | None) -> dict[str, Any]:
    contract = contract_for_article(article)
    return {
        "version": EDITORIAL_CONTRACT_VERSION,
        "profile": contract.profile,
        "minimum_supported_fact_units": contract.minimum_supported_fact_units,
        "fact_unit_definition": (
            "主体动作算1个事实点；facts中每条必须增加不同的对象、数字、产品能力、场景、进度或限制，"
            "同义复述不算新事实点。"
        ),
        "candidate_rule": (
            "主体动作加独立facts不足最低事实点时，candidate_verdict.usable必须为false，"
            "reason_code使用insufficient_facts。"
        ),
    }


def writing_prompt_contract(article: dict[str, Any] | None) -> dict[str, Any]:
    contract = contract_for_article(article)
    return {
        **contract_metadata(article),
        "principle": "固定信息结构，不固定句式、连接词或段落开头。",
        "sentence_jobs": list(contract.sentence_jobs),
        "domain_rules": list(contract.domain_rules),
        "source_rules": [
            "所有可核验信息只能来自当前新闻的fact_card。",
            "正文不重复媒体名、链接或‘据某媒体报道’；来源由渲染层单独显示。",
            "事实不足时不要凑字，应让该候选退出并由同日reserve补位。",
        ],
        "positive_example": {
            "notice": "仅示范信息结构和密度，不得复制其中事实、主体或措辞。",
            "detail_heading": contract.positive_example_heading,
            "reader_summary": contract.positive_example_summary,
        },
        "negative_examples": list(NEGATIVE_EXAMPLES),
    }


def repair_prompt_contract(article: dict[str, Any] | None) -> dict[str, Any]:
    return {
        **writing_prompt_contract(article),
        "repair_reason_rules": {
            "missing_item": "补齐该索引，并完整输出标题和正文。",
            "missing_or_incomplete": "重新写完整正文，不沿用残句。",
            "insufficient_depth": "只用尚未写出的独立事实补足信息，不重复已有句子。",
            "too_long": "删除次要信息并保留完整事实句，不从句中间截断。",
            "numeric_fact_mismatch": "逐字核对fact_card.numbers，只保留来源支持的数字和单位。",
            "heading_too_short": "把主体、动作和关键结果写完整，不增加来源外事实。",
            "heading_too_long": "保留主体、动作和最有信息量的结果，删除次要修饰。",
            "english_fragment": "改写为自然中文核心句，只保留必要的品牌或产品英文名。",
            "nested_brackets": "输出纯文本标题，不要自行添加【】。",
            "banned_phrase": "删除模板话，改为来源支持的具体事实。",
            "generic_editorial_filler": "删除行业判断，换成fact_card中的动作、数字、场景或进度。",
            "promotional_copy": "删除广告、导流和购买建议，只保留新闻事实。",
            "incomplete_ending": "重写最后一句并用完整标点结束。",
            "contract_mismatch": "逐项核对编辑合同后重写，不解释检查过程。",
        },
    }


def review_prompt_contract(article: dict[str, Any] | None) -> dict[str, Any]:
    contract = contract_for_article(article)
    return {
        **contract_metadata(article),
        "review_rules": [
            "逐条确认每个完整句子都增加了来源支持的新事实，不把同义反复算作新信息。",
            "目标字数用于指导写作，不是独立拒绝条件；达到accept底线且事实完整时，不得只因低于target而拒绝。",
            "正文字数达到门槛但主要由评论、预测、模板话或重复内容组成时仍不合格。",
            "事实不足应建议drop_item并由reserve补位，不建议用空话扩写。",
            "风格问题必须引用终稿中的逐字证据；没有证据不得拒绝。",
        ],
        "profile_rules": list(contract.domain_rules),
    }


def reader_summary_depth_report(
    value: str,
    contract: EditorialContract | None = None,
) -> dict[str, Any]:
    selected = contract or TECH_BRIEFING_CONTRACT
    text = str(value or "").strip()
    effective_chars = len(re.findall(r"[A-Za-z0-9\u3400-\u9fff]", text))
    complete_sentences = re.findall(r"[^。！？!?]+[。！？!?][”’\"']?", text)
    sentence_count = len(complete_sentences)
    enough_chars = effective_chars >= selected.reader_summary_accept_min_chars
    enough_sentences = sentence_count >= selected.reader_summary_min_sentences
    dense_enough = (
        sentence_count >= selected.reader_summary_preferred_min_sentences
        or effective_chars >= selected.reader_summary_two_sentence_min_chars
    )
    return {
        "ok": enough_chars and enough_sentences and dense_enough,
        "effective_chars": effective_chars,
        "complete_sentences": sentence_count,
        "minimum_effective_chars": selected.reader_summary_accept_min_chars,
        "two_sentence_minimum_effective_chars": selected.reader_summary_two_sentence_min_chars,
        "minimum_sentences": selected.reader_summary_min_sentences,
        "preferred_sentences": selected.reader_summary_preferred_min_sentences,
    }


def fact_card_evidence_units(card: dict[str, Any] | None) -> list[str]:
    if not isinstance(card, dict):
        return []
    candidates = [str(card.get("action", "")).strip()]
    facts = card.get("facts", [])
    if isinstance(facts, list):
        candidates.extend(str(value).strip() for value in facts)

    units: list[str] = []
    normalized_units: list[str] = []
    for value in candidates:
        normalized = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value.lower())
        if len(normalized) < 6:
            continue
        if any(
            normalized == existing
            or (len(normalized) >= 6 and normalized in existing)
            or (len(existing) >= 6 and existing in normalized)
            for existing in normalized_units
        ):
            continue
        units.append(value)
        normalized_units.append(normalized)
    return units


def fact_card_readiness_report(
    card: dict[str, Any] | None,
    contract: EditorialContract | None = None,
) -> dict[str, Any]:
    selected = contract or TECH_BRIEFING_CONTRACT
    units = fact_card_evidence_units(card)
    return {
        "ok": len(units) >= selected.minimum_supported_fact_units,
        "supported_fact_units": len(units),
        "minimum_supported_fact_units": selected.minimum_supported_fact_units,
        "units": units,
    }
