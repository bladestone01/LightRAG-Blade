from __future__ import annotations
from typing import Any

GRAPH_FIELD_SEP = "<SEP>"

PROMPTS: dict[str, Any] = {}

PROMPTS["DEFAULT_LANGUAGE"] = "English"
PROMPTS["DEFAULT_TUPLE_DELIMITER"] = "<|>"
PROMPTS["DEFAULT_RECORD_DELIMITER"] = "##"
PROMPTS["DEFAULT_COMPLETION_DELIMITER"] = "<|COMPLETE|>"

PROMPTS["DEFAULT_ENTITY_TYPES"] = ["机构", "人员", "检测方法", "标准类型", "具体标准名称", "标准编码", "名称", "分类", "规范要求"]

PROMPTS["entity_extraction"] = """
- Role: 知识图谱构建专家和生物安全与食品安全国家标准分析师
- Background: 用户希望通过从生物安全和食品安全国家标准文件中提取知识图谱的三元组，挖掘国标发布的规律，从而更好地理解标准中的关键信息和逻辑关系，为相关领域的研究和实践提供支持。
- Profile: 你是一位在知识图谱构建以及生物安全和食品安全国家标准分析领域具有深厚专业知识和丰富实践经验的专家，能够精准地从复杂的标准文件中提取有价值的信息，并将其转化为结构化的知识图谱三元组。
- Skills: 你具备以下关键能力：
    - 深入理解生物安全和食品安全国家标准文件的结构、内容以及发布规律。
    - 熟练掌握知识图谱构建技术，能够高效识别和提取关键实体、关系和属性。
    - 能够将复杂的文本信息准确转化为简洁明了的三元组形式，同时确保信息的完整性和准确性。
- Goals:
    1. 从标准文件中全面提取关键实体，涵盖标准类型（国标/行标/地标/团标）、标准一级分类、标准二级分类、标准名称、标准编号、发布部门等标准相关信息，对于特定标准，还需提取使用范围、技术要求、检验方法等关键信息。
    2. 精准确定实体之间的关系，如"包括","发布","适用于","应符合","测定方法","检测指标", "编码为"等。
    3. 为每个实体和关系赋予必要的属性，如指标数值、检验方法的具体步骤等，对于具体的检测方法以及指标，提取关键信息即可。
    4. 将提取的实体和关系整理为有效的三元组形式，便于进一步分析和应用，为挖掘国标发布的规律提供有力支持。
- Constrains: 提取的三元组必须准确反映标准文件的核心内容，确保信息的完整性和准确性，避免遗漏重要信息，同时保持简洁明了，便于理解和使用。
输出语言使用{language}。
- Workflow:
1. 识别所有实体。为每个实体提取以下信息：
- entity_name: 实体名称（保持原文语言。）
- entity_type: 实体类型（从以下选项中选择：[{entity_types}]）
- entity_description: 对实体属性及活动的完整描述
将每个实体格式化为：("entity"{tuple_delimiter}<entity_name>{tuple_delimiter}<entity_type>{tuple_delimiter}<entity_description>)

2. 从步骤1识别的实体中，找出所有存在明确关联的（源实体，目标实体）组合(source_entity, target_entity)。
为每对关联实体提取以下信息：
- source_entity: 源实体名称（来自步骤1）
- target_entity: 目标实体名称（来自步骤1）
- relationship_description: 说明你认为源实体和目标实体关联的原因
- relationship_strength: 表示源实体和目标实体关联强度的数值[1-10]
- relationship_keywords: 一个或多个概括关系总体性质的高层次关键词，侧重于概念或主题，而不是具体细节
将每个关系格式化为：("relationship"{tuple_delimiter}<source_entity>{tuple_delimiter}<target_entity>{tuple_delimiter}<relationship_description>{tuple_delimiter}<relationship_keywords>{tuple_delimiter}<relationship_strength>)

3. 找出概括整篇文章的主要概念或主题的高层次关键词(high_level_keywords)。这些关键词应该抓住文档中呈现的总体思想。
将内容关键词格式化为：("content_keywords"{tuple_delimiter}<high_level_keywords>)

4. 用{language}输出步骤1-2的所有结果，使用**{record_delimiter}**作为分隔符。

5. 完成后输出{completion_delimiter}

######################
---示例---
######################
{examples}

#############################
---真实数据---
######################
实体类型: [{entity_types}]
文本内容:
{input_text}
######################
输出:"""

PROMPTS["entity_extraction_examples"] = [
    """示例1:
实体类型: ["机构", "人员", "检测方法", "标准类型", "具体标准名称", "标准编码", "名称", "分类", "规范要求"]
Text:
```
## 中华人民共和国卫生行业标准
#### WS/T 305—2023
代替WS/T 305-2009
# 卫生健康信息数据集元数据标准
#### Metadata specification of health information dataset
#### 2023-08-07 发布 2024-02-01 实施
#### 中华人民共和国国家卫生健康委员会 发布
```
Output:
("entity"{tuple_delimiter}"中华人民共和国国家卫生健康委员会"{tuple_delimiter}"机构"{tuple_delimiter}"负责发布中华人民共和国卫生行业标准的机构。){record_delimiter}
("entity"{tuple_delimiter}"中华人民共和国卫生行业标准"{tuple_delimiter}"标准类型"{tuple_delimiter}"本标准的编号为WS/T 305—2023，规定卫生健康信息数据集的元数据标准，用于指导数据集规范化管理){record_delimiter}
("entity"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"具体标准名称"{tuple_delimiter}"本标准的编号为WS/T 305—2023，规定卫生健康信息数据集的元数据标准，用于指导数据集规范化管理){record_delimiter}
("entity"{tuple_delimiter}"WS/T 305-2009"{tuple_delimiter}"标准编号"{tuple_delimiter}"被新版本标准WS/T 305—2023代替的旧版本标准。){record_delimiter}
("entity"{tuple_delimiter}"Metadata specification of health information dataset"{tuple_delimiter}"名称"{tuple_delimiter}"卫生健康信息数据集元数据标准的英文标准名称){record_delimiter}
("entity"{tuple_delimiter}"WS/T 305—2023"{tuple_delimiter}"标准编号"{tuple_delimiter}"现行有效的卫生健康信息数据集元数据标准编号。){record_delimiter}
("entity"{tuple_delimiter}"卫生健康信息数据集"{tuple_delimiter}"名称"{tuple_delimiter}"卫生健康信息的数据集合，是元数据描述的核心对象。){record_delimiter}
("entity"{tuple_delimiter}"元数据"{tuple_delimiter}"名称"{tuple_delimiter}"描述数据的数据，用于定义和描述卫生健康信息数据集的内容、结构及相关属性。){record_delimiter}
("entity"{tuple_delimiter}"2023-08-07"{tuple_delimiter}"日期"{tuple_delimiter}"标准发布的日期，标志着该标准正式生效的时间点。){record_delimiter}
("entity"{tuple_delimiter}"2024-02-01"{tuple_delimiter}"日期"{tuple_delimiter}"标准实施的日期，表明该标准从此时起开始执行。){record_delimiter}
("relationship"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"WS/T 305—2023"{tuple_delimiter}"具体标准名称与标准编号的映射关系"{tuple_delimiter}"编码为"{tuple_delimiter}10){completion_delimiter}
("relationship"{tuple_delimiter}"中华人民共和国国家卫生健康委员会"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"发布关系"{tuple_delimiter}"发布"{tuple_delimiter}10){completion_delimiter}
("relationship"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"卫生健康信息数据集"{tuple_delimiter}"描述关系"{tuple_delimiter}"描述"{tuple_delimiter}10){completion_delimiter}
("relationship"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"元数据"{tuple_delimiter}"包括关系"{tuple_delimiter}"包括"{tuple_delimiter}10){completion_delimiter}
("relationship"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"2023-08-07"{tuple_delimiter}"发布日期关系"{tuple_delimiter}"发布日期"{tuple_delimiter}10){completion_delimiter}
("relationship"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"2024-02-01"{tuple_delimiter}"实施日期关系"{tuple_delimiter}"实施日期"{tuple_delimiter}10){completion_delimiter}
("relationship"{tuple_delimiter}"WS/T 305—2023"{tuple_delimiter}"WS/T 305-2009"{tuple_delimiter}"新版标准替代旧版标准文件"{tuple_delimiter}"代替"{tuple_delimiter}9){completion_delimiter}
("relationship"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"Metadata specification of health information dataset"{tuple_delimiter}"标准包含中英文名称对应关系"{tuple_delimiter}"包括"{tuple_delimiter}8){completion_delimiter}
("content_keywords"{tuple_delimiter}"卫生健康信息数据集，元数据标准，标准发布，标准实施，替代关系"){completion_delimiter}
#############################""",
    """Example 2:
实体类型: ["机构", "人员", "检测方法", "标准类型", "具体标准名称", "标准编码", "名称", "分类", "规范要求"]
Text:
```
 卫生健康信息数据集元数据标准标准为推荐性标准, 此标准代替WS/T 305－2009 《卫生信息数据集元数据规范》。与WS/T 305－2009相比，主要为
编辑性修改。此标准由国家卫生健康标准委员会卫生健康信息标准专业委员会负责技术审查和技术咨询，由国家卫生健康委统计信息中心负责协调性和格式审查，由国家卫生健康委规划发展与信息化司负责
业务管理、法规司负责统筹管理。标准起草单位：中国人民解放军总医院、国家卫生健康委统计信息中心。标准主要起草人：刘建超、胡建平。
```
Output:
("entity"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"具体标准名称"{tuple_delimiter}"本标准为推荐性标准，代替WS/T 305－2009，由多个单位和技术人员共同起草。"){completion_delimiter}
("entity"{tuple_delimiter}"WS/T 305－2009"{tuple_delimiter}"标准编号"{tuple_delimiter}"旧版标准，被本标准替代，内容涉及卫生信息数据集元数据规范。"){completion_delimiter}
("entity"{tuple_delimiter}"卫生信息数据集元数据规范"{tuple_delimiter}"规范要求"{tuple_delimiter}"与WS/T 305－2009相对应的规范要求内容。"){completion_delimiter}
("entity"{tuple_delimiter}"国家卫生健康标准委员会卫生健康信息标准专业委员会"{tuple_delimiter}"机构"{tuple_delimiter}"负责本标准的技术审查和技术咨询工作。"){completion_delimiter}
("entity"{tuple_delimiter}"国家卫生健康委统计信息中心"{tuple_delimiter}"机构"{tuple_delimiter}"负责本标准的协调性和格式审查工作。"){completion_delimiter}
("entity"{tuple_delimiter}"国家卫生健康委规划发展与信息化司"{tuple_delimiter}"机构"{tuple_delimiter}"负责本标准的业务管理工作。"){completion_delimiter}
("entity"{tuple_delimiter}"国家卫生健康委法规司"{tuple_delimiter}"机构"{tuple_delimiter}"负责本标准的统筹管理工作。"){completion_delimiter}
("entity"{tuple_delimiter}"中国人民解放军总医院"{tuple_delimiter}"机构"{tuple_delimiter}"参与本标准的起草工作。"){completion_delimiter}
("entity"{tuple_delimiter}"刘建超"{tuple_delimiter}"人员"{tuple_delimiter}"本标准的主要起草人之一。"){completion_delimiter}
("entity"{tuple_delimiter}"胡建平"{tuple_delimiter}"人员"{tuple_delimiter}"本标准的主要起草人之一。"){completion_delimiter}
("relationship"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"WS/T 305－2009"{tuple_delimiter}"卫生健康信息数据集元数据标准代替WS/T 305－2009，两者属于前后版本关系。"{tuple_delimiter}"代替"{tuple_delimiter}10){completion_delimiter}
("relationship"{tuple_delimiter}"国家卫生健康标准委员会卫生健康信息标准专业委员会"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"负责本标准的技术审查和技术咨询工作，与本标准存在业务管理关系。"{tuple_delimiter}"技术审查"{tuple_delimiter}8){completion_delimiter}
("relationship"{tuple_delimiter}"国家卫生健康委统计信息中心"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"负责本标准的协调性和格式审查工作，与本标准存在业务管理关系。"{tuple_delimiter}"协调审查"{tuple_delimiter}8){completion_delimiter}
("relationship"{tuple_delimiter}"国家卫生健康委规划发展与信息化司"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"负责本标准的业务管理工作，与本标准存在业务管理关系。"{tuple_delimiter}"业务管理"{tuple_delimiter}8){completion_delimiter}
("relationship"{tuple_delimiter}"国家卫生健康委法规司"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"负责本标准的统筹管理工作，与本标准存在业务管理关系。"{tuple_delimiter}"统筹管理"{tuple_delimiter}8){completion_delimiter}
("relationship"{tuple_delimiter}"中国人民解放军总医院"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"参与本标准的起草工作，与本标准存在起草关系。"{tuple_delimiter}"起草"{tuple_delimiter}9){completion_delimiter}
("relationship"{tuple_delimiter}"刘建超"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"为主要起草人之一，与本标准存在起草关系。"{tuple_delimiter}"起草"{tuple_delimiter}9){completion_delimiter}
("relationship"{tuple_delimiter}"胡建平"{tuple_delimiter}"卫生健康信息数据集元数据标准"{tuple_delimiter}"为主要起草人之一，与本标准存在起草关系。"{tuple_delimiter}"起草"{tuple_delimiter}9){completion_delimiter}
("content_keywords"{tuple_delimiter}"推荐性标准, 标准替代, 技术审查, 协调审查, 业务管理, 统筹管理, 标准起草, 主要起草人"){completion_delimiter}
#############################""",
]

PROMPTS[
    "summarize_entity_descriptions"
] = """你是一个负责生成数据综合摘要的智能助手。给定一个或两个实体及其相关描述列表（所有描述均针对同一实体或实体组），请将所有描述整合成一份全面的综合描述。确保包含所有描述中的信息。
如果提供的描述存在矛盾，请协调矛盾并生成连贯的摘要。请使用第三人称撰写，并包含实体名称以保持完整上下文。输出语言使用{language}。

#######
---输入数据---
实体: {entity_name}
描述列表: {description_list}
#######
输出:
""

PROMPTS["entity_continue_extraction"] = ""
注意：上次提取遗漏了大量实体和关系。

---记住步骤---

1. 识别所有实体。为每个实体提取以下信息：
- entity_name: 实体名称（保持原文语言。）
- entity_type: 实体类型（从以下选项中选择：[{entity_types}]）
- entity_description: 对实体属性及活动的完整描述
实体格式：("entity"{tuple_delimiter}"<entity_name>"{tuple_delimiter}"<entity_type>"{tuple_delimiter}"<entity_description>)

2. 从步骤1识别的实体中，找出所有存在明确关联的（源实体，目标实体）组合(source_entity, target_entity)。
为每对关联实体提取以下信息：
- source_entity: 源实体名称（来自步骤1）
- target_entity: 目标实体名称（来自步骤1）
- relationship_description: 说明你认为源实体和目标实体关联的原因
- relationship_strength: 表示源实体和目标实体关联强度的数值
- relationship_keywords: 一个或多个概括关系总体性质的高层次关键词，侧重于概念或主题，而不是具体细节
关系格式：("relationship"{tuple_delimiter}"<source_entity>"{tuple_delimiter}"<target_entity>"{tuple_delimiter}"<relationship_description>"{tuple_delimiter}"<relationship_keywords>"{tuple_delimiter}"<relationship_strength>)

3. 找出概括整篇文章的主要概念或主题的高层次关键词(high_level_keywords)。这些关键词应该抓住文档中呈现的总体思想。
内容关键词格式：("content_keywords"{tuple_delimiter}"<high_level_keywords>)

4. 用{language}输出步骤1-2的所有结果，使用**##**作为分隔符。

5. 完成后输出{completion_delimiter}


---输出---

请按相同格式补充遗漏内容：\n
"".strip()

PROMPTS["entity_if_loop_extraction"] = ""
---目标---'

似乎还有一些实体没有被发现。

---输出---

如果仍然有需要添加的实体，回答“YES”或“NO”（只回答“YES”或“NO”）。
"".strip()

PROMPTS["fail_response"] = (
    "抱歉，我无法回答这个问题。[无相关上下文]"
)

PROMPTS["rag_response"] = ""---角色---

你是一位智能助手，负责根据提供的知识库回答用户查询。


---目标---

基于知识库内容和响应规则生成简明回答，需综合考虑对话历史和当前查询。整合知识库中所有相关信息，并融入与知识库相关的常识。请勿包含知识库未提供的信息。

处理含时间戳的关系时：
1. 每个关系都带有"created_at"时间戳，表示该知识的获取时间
2. 遇到冲突关系时，需同时考虑语义内容和时间戳
3. 不要自动优先选择最新关系 - 应根据上下文进行判断
4. 对于时间敏感查询，优先考虑内容中的时间信息，其次再考虑创建时间戳

---对话历史---
{history}

---知识库---
{context_data}

---回复规则---

- 目标格式和长度: {response_type}
- 使用带章节标题的markdown格式
- 使用与用户问题相同的语言回答
- 确保回答与对话历史保持连贯性
- 在"参考文献"章节末尾列出最多5个重要参考来源。需明确标注每个来源是来自知识图谱(KG)还是向量数据(DC)，并包含文件路径（如有），格式如下：[KG/DC] 来源内容 (File: file_path)
- 如不知道答案，请直接说明
- 请勿编造信息。不要包含知识库未提供的内容""

PROMPTS["keywords_extraction"] = ""---角色---

你是一位负责从用户查询和对话历史中提取高层次和底层次关键词的智能助手。

---目标---

根据查询内容和对话历史，列出高层次和底层次关键词。高层次关键词关注整体概念或主题，底层次关键词关注具体实体、细节或明确术语。

---说明---

- 提取关键词时需同时考虑当前查询和相关对话历史
- 以JSON格式输出关键词，输出内容将被JSON解析器处理，请勿添加任何额外内容
- JSON应包含两个键：
  - "high_level_keywords" 用于整体概念或主题
  - "low_level_keywords" 用于具体实体或细节

######################
---示例---
######################
{examples}

#############################
---实际数据---
######################
对话历史:
{history}

当前查询: {query}
######################
输出应为人类可读文本（非unicode字符）。保持与查询相同的语言。
输出:

""

PROMPTS["keywords_extraction_examples"] = [
    ""示例1::

查询: "国际贸易如何影响全球经济稳定性"
################
Output:
{
  "high_level_keywords": ["国际贸易", "全球经济稳定性", "经济影响"],
  "low_level_keywords":  ["贸易协定", "关税", "货币汇率", "进口", "出口"]
}
#############################"",
    ""示例2:

查询: "森林砍伐对生物多样性有哪些环境影响？"
################
输出:
{
  "high_level_keywords": ["环境影响", "森林砍伐", "生物多样性丧失"],
  "low_level_keywords":  ["物种灭绝", "栖息地破坏", "碳排放", "雨林", "生态系统"]
}
#############################"",
    ""示例3:

查询: "教育在减少贫困中扮演什么角色？"
################
输出:
{
  "high_level_keywords": ["教育", "减贫", "社会经济发展"],
  "low_level_keywords":  ["入学机会", "识字率", "职业培训", "收入不平等"]
}
#############################"",
]


PROMPTS["naive_rag_response"] = ""---角色---

你是一位智能助手，负责根据下述提供的文本块回答用户查询。

---目标---

基于文本块内容和响应规则生成简明回答，需综合考虑对话历史和当前查询。整合文本块中所有相关信息，并融入与文本块相关的常识。请勿包含文本块未提供的信息。

处理含时间戳的关系时：
1. 每个关系都带有"created_at"时间戳，表示该知识的获取时间
2. 遇到冲突关系时，需同时考虑语义内容和时间戳
3. 不要自动优先选择最新关系 - 应根据上下文进行判断
4. 对于时间敏感查询，优先考虑内容中的时间信息，其次再考虑创建时间戳

---对话历史---
{history}

---文本块---
{content_data}

---回复规则---

- 目标格式和长度: {response_type}
- 使用带章节标题的markdown格式
- 使用与用户问题相同的语言回答
- 确保回答与对话历史保持连贯性
- 在"参考文献"章节末尾列出最多5个重要参考来源。需明确标注每个来源是来自知识图谱(KG)还是向量数据(DC)，并包含文件路径（如有），格式如下：[KG/DC] 来源内容 (File: file_path)
- 如不知道答案，请直接说明
- 请勿编造信息。不要包含知识库未提供的内容""


PROMPTS[
    "similarity_check"
] = ""请分析以下两个问题的相似度:

问题1: {original_prompt}
问题2: {cached_prompt}

请评估这两个问题是否语义相似，以及问题2的答案能否用于回答问题1，直接给出0到1之间的相似度分数。

相似度评分标准:
0: 完全不相关或答案无法复用，包括但不限于:
   - 问题主题不同
   - 问题中提及的地点不同
   - 问题中提及的时间不同
   - 问题中提及的具体人物不同
   - 问题中提及的具体事件不同
   - 问题中的背景信息不同
   - 问题中的关键条件不同
1: 完全相同且答案可直接复用
0.5: 部分相关且答案需要修改后才能使用
仅返回0-1之间的数字，不要包含任何额外内容。
""

PROMPTS["mix_rag_response"] = ""---角色---

你是一位智能助手，负责根据提供的数据源回答用户查询。


---目标---

基于数据源内容和响应规则生成简明回答，需综合考虑对话历史和当前查询。数据源包含两部分：知识图谱(KG)和文档片段(DC)。整合数据源中所有相关信息，并融入相关常识。请勿包含数据源未提供的信息。

处理含时间戳的关系时：
1. 每个关系都带有"created_at"时间戳，表示该知识的获取时间
2. 遇到冲突关系时，需同时考虑语义内容和时间戳
3. 不要自动优先选择最新关系 - 应根据上下文进行判断
4. 对于时间敏感查询，优先考虑内容中的时间信息，其次再考虑创建时间戳

---对话历史---
{history}

---数据源---

1. 来自知识图谱(KG):
{kg_context}

2. 来自文档片段(DC):
{vector_context}

---回复规则---

- 目标格式和长度: {response_type}
- 使用带章节标题的markdown格式
- 使用与用户问题相同的语言回答
- 确保回答与对话历史保持连贯性
- 按回答要点分章节组织内容
- 使用清晰描述性的章节标题反映内容
- 在"参考文献"章节末尾列出最多5个重要参考来源。需明确标注每个来源是来自知识图谱(KG)还是向量数据(DC)，并包含文件路径（如有），格式如下：[KG/DC] 来源内容 (File: file_path)
- 如不知道答案，请直接说明，请勿编造信息
- 不要包含数据源未提供的内容"""
