from __future__ import annotations
from typing import Any

GRAPH_FIELD_SEP = "<SEP>"

PROMPTS: dict[str, Any] = {}

PROMPTS["DEFAULT_LANGUAGE"] = "English"
PROMPTS["DEFAULT_TUPLE_DELIMITER"] = "<|>"
PROMPTS["DEFAULT_RECORD_DELIMITER"] = "##"
PROMPTS["DEFAULT_COMPLETION_DELIMITER"] = "<|COMPLETE|>"

PROMPTS["DEFAULT_ENTITY_TYPES"] = ["机构", "人员", "检测方法", "标准类型", "具体标准名称", "标准编码", "名称", "分类", "规范要求", "日期",  "检测设备"]

PROMPTS["DEFAULT_USER_PROMPT"] = "n/a"

PROMPTS["entity_extraction"] = """
- Role: 知识图谱构建专家和生物安全与食品安全国家标准分析师
- Background: 用户希望通过提取知识图谱三元组
- Profile: 精准转化标准文件为结构化知识图谱的专家
- Skills: 具备以下关键能力：
    - 深度理解国家标准文件结构
    - 熟练高效提取实体、关系和属性
    - 确保三元组准确性与完整性
- Goals:
    1. 取实体（标准类型、名称、编号、技术要求等）
    2. 确定关系（使用主动动词且方向正确）
    3. 赋予必要属性
    4. 整理为有效三元组
- Constrains: 
   1  三元组必须准确反映标准文件的核心内容，确保信息的完整性和准确性，避免遗漏重要信息,保持简洁明了
   2 中国人民共和国国家标准、中华人民共和国XXX行业标准、XXX省市地方标准、XXX团体标准，分别简称为国家标准、行业标准、地方标准、团体标准，它们在实体中被定义为标准类型。
   3 每次提取实体最多不超过20个，关系最多不超过30个
  
输出语言使用{language}。
- Workflow:
1. 识别所有实体, 实体必须符合实体类型:["机构", "人员", "检测方法", "标准类型", "具体标准名称", "标准编码", "名称", "分类", "规范要求", "日期",  "检测设备"]。为每个实体提取以下信息：
- entity_name: 实体名称（保持原文语言。）
- entity_type: 实体类型（从以下选项中选择：[{entity_types}]）
- entity_description: 对实体属性及活动的完整描述
将每个实体格式化为：("entity"{tuple_delimiter}<entity_name>{tuple_delimiter}<entity_type>{tuple_delimiter}<entity_description>).
  实体数量最多不超过20个.

2. 从步骤1识别的实体中，找出所有存在明确关联的（源实体，目标实体）组合(source_entity, target_entity)。
为每对关联实体提取以下信息：
- source_entity: 源实体名称（来自步骤1）
- target_entity: 目标实体名称（来自步骤1）
- relationship_description: 说明你认为源实体和目标实体关联的原因
- relationship_strength: 表示源实体和目标实体关联强度的数值[1-10]
- relationship_keywords: 一个或多个概括关系总体性质的高层次关键词，侧重于概念或主题，而不是具体细节
  输出结果规则: 
    a.将每个关系输出严格遵守如下格式：("relationship"<|><source_entity><|><target_entity><|><relationship_description><|><relationship_keywords><|><relationship_strength>)
    b. 提取关系最多不超过30个.
    c.关系方向规则: 'source_entity'必须是**能主动执行动作**的实体（如机构/标准）,`target_entity`必须是**动作接受者**（如日期/指标）,输出前验证：若`source_entity`无法执行该动作（如"日期"不能"发布"），则删除三元组. 
    d.关系动词规范 : 仅使用1-2个主动语态动词，禁用被动词汇;强制动词映射表:
       | 禁止词 | 替换词 | 适用场景               |
       |--------|--------|----------------------|
       | 归属于 | 分类为 | 标准→类型             |
       | 发布于 | 发布   | 标准→日期            |
       | 生效于 | 生效   | 标准→日期            |
       | 适用于 | 规范   | 标准→对象            |
       | 描述   | 包含   | 标准→技术要求         |
       
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
"""

PROMPTS["entity_extraction_examples"] = [
    """示例1:
   实体类型: ["机构", "人员", "检测方法", "标准类型", "具体标准名称", "标准编码", "名称", "分类", "规范要求", "日期", "检测设备"]
   Text:
   ```
   ## 中华人民共和国卫生行业标准
   #### WS/T 961—2023
   代替WS/T 911-2003
   # 食品安全国家标准 食品添加剂 水杨酸
   #### National Food Safety Standard: Food Additive Salicylic Acid
   #### 2023-08-07 发布 2024-02-01 实施
   #### 中华人民共和国国家卫生部 发布
   ```
   Output:
   ("entity"{tuple_delimiter}"中华人民共和国国家卫生部"{tuple_delimiter}"机构"{tuple_delimiter}"管理卫生行业的机构"){record_delimiter}
   ("entity"{tuple_delimiter}"中华人民共和国卫生行业标准"{tuple_delimiter}"标准类型"{tuple_delimiter}"用以分类不同的标准类型"){record_delimiter}
   ("entity"{tuple_delimiter}"食品安全国家标准 食品添加剂 水杨酸"{tuple_delimiter}"具体标准名称"{tuple_delimiter}"标准全称，编号为WS/T 961—2023"){record_delimiter}
   ("entity"{tuple_delimiter}"WS/T 961—2023"{tuple_delimiter}"标准编号"{tuple_delimiter}"标准编号"){record_delimiter}
   ("entity"{tuple_delimiter}"WS/T 911-2003"{tuple_delimiter}"标准编号"{tuple_delimiter}"旧版本标准"){record_delimiter}
   ("entity"{tuple_delimiter}"National Food Safety Standard: Food Additive Salicylic Acid"{tuple_delimiter}"名称"{tuple_delimiter}"英文名称"){record_delimiter}
   ("entity"{tuple_delimiter}"食品添加剂"{tuple_delimiter}"名称"{tuple_delimiter}"标准分类"){record_delimiter}
   ("entity"{tuple_delimiter}"2023-08-07"{tuple_delimiter}"日期"{tuple_delimiter}"标准发布日期"){record_delimiter}
   ("entity"{tuple_delimiter}"2024-02-01"{tuple_delimiter}"日期"{tuple_delimiter}"标准实施日期"){record_delimiter}
   ("entity"{tuple_delimiter}"GB/T 11538—2006"{tuple_delimiter}"检测方法""{tuple_delimiter}"测定水杨酸甲酯含量的方法标准"){record_delimiter}
   ("relationship"{tuple_delimiter}"食品安全国家标准 食品添加剂 水杨酸"{tuple_delimiter}"WS/T 961—2023"{tuple_delimiter}"具体标准名称与标准编号的对应关系"{tuple_delimiter}"编号为"{tuple_delimiter}10){completion_delimiter}
   ("relationship"{tuple_delimiter}"中华人民共和国国家卫生部"{tuple_delimiter}"食品安全国家标准 食品添加剂 水杨酸"{tuple_delimiter}"机构发布具体标准的关系"{tuple_delimiter}"发布"{tuple_delimiter}10){completion_delimiter}
   ("relationship"{tuple_delimiter}"水杨酸"{tuple_delimiter}"食品添加剂"{tuple_delimiter}"物质分类"{tuple_delimiter}"分类为"{tuple_delimiter}10){completion_delimiter}
   ("relationship"{tuple_delimiter}"食品安全国家标准 食品添加剂 水杨酸"{tuple_delimiter}"中华人民共和国卫生行业标准"{tuple_delimiter}"该标准的标准类型"{tuple_delimiter}"分类为"{tuple_delimiter}10){completion_delimiter}
   ("relationship"{tuple_delimiter}"2023-08-07"{tuple_delimiter}"食品安全国家标准 食品添加剂 水杨酸"{tuple_delimiter}"标准发布日期"{tuple_delimiter}"发布"{tuple_delimiter}10){completion_delimiter}
   ("relationship"{tuple_delimiter}"2024-02-01"{tuple_delimiter}"食品安全国家标准 食品添加剂 水杨酸"{tuple_delimiter}"标准生效日期"{tuple_delimiter}"生效"{tuple_delimiter}10){completion_delimiter}
   ("relationship"{tuple_delimiter}"WS/T 961—2023"{tuple_delimiter}"WS/T 911-2003"{tuple_delimiter}"新版标准替代旧版标准文件"{tuple_delimiter}"代替"{tuple_delimiter}9){completion_delimiter}
   ("relationship"{tuple_delimiter}"食品安全国家标准 食品添加剂 水杨酸"{tuple_delimiter}"National Food Safety Standard: Food Additive Salicylic Acid"{tuple_delimiter}"中英文名称对应"{tuple_delimiter}"翻译为"{tuple_delimiter}8){completion_delimiter}
   ("relationship"{tuple_delimiter}"食品安全国家标准 食品添加剂 水杨酸"{tuple_delimiter}"附录A"{tuple_delimiter}"包含附录内容"{tuple_delimiter}"包含"{tuple_delimiter}10){completion_delimiter}
   ("content_keywords"{tuple_delimiter}"食品添加剂, 水杨酸，标准发布，标准实施，替代关系"){completion_delimiter}
#############################""",
    """Example 2:
实体类型: ["机构", "人员", "检测方法", "标准类型", "具体标准名称", "标准编码", "名称", "分类", "规范要求", "日期"]
Text:
```
 卫生健康信息数据集元数据规范为推荐性标准, 此标准代替WS/T 305－2009 《卫生信息数据集元数据规范》。与WS/T 305－2009相比，主要为
编辑性修改。此标准由国家卫生健康标准委员会卫生健康信息标准专业委员会负责技术审查，法规司负责统筹管理。标准起草单位：中国人民解放军总医院。标准主要起草人：刘建超。
```
Output:
("entity"{tuple_delimiter}"卫生健康信息数据集元数据规范"{tuple_delimiter}"具体标准名称"{tuple_delimiter}"具体标准名称"){completion_delimiter}
("entity"{tuple_delimiter}"WS/T 305－2009"{tuple_delimiter}"标准编号"{tuple_delimiter}"旧版标准编号"){completion_delimiter}
("entity"{tuple_delimiter}"卫生信息数据集元数据规范"{tuple_delimiter}"规范要求"{tuple_delimiter}"规范要求内容"){completion_delimiter}
("entity"{tuple_delimiter}"国家卫生健康标准委员会卫生健康信息标准专业委员会"{tuple_delimiter}"机构"{tuple_delimiter}"标准的技术审查和技术咨询机构"){completion_delimiter}
("entity"{tuple_delimiter}"国家卫生健康委统计信息中心"{tuple_delimiter}"机构"{tuple_delimiter}"协调标准和格式审查"){completion_delimiter}
("entity"{tuple_delimiter}"国家卫生健康委法规司"{tuple_delimiter}"机构"{tuple_delimiter}"标准的统筹管理"){completion_delimiter}
("entity"{tuple_delimiter}"刘建超"{tuple_delimiter}"人员"{tuple_delimiter}"标准起草人之一"){completion_delimiter}
("relationship"{tuple_delimiter}"卫生健康信息数据集元数据规范"{tuple_delimiter}"WS/T 305－2009"{tuple_delimiter}"前后版本关系"{tuple_delimiter}"代替"{tuple_delimiter}10){completion_delimiter}
("relationship"{tuple_delimiter}"国家卫生健康标准委员会卫生健康信息标准专业委员会"{tuple_delimiter}"卫生健康信息数据集元数据规范"{tuple_delimiter}"标准技术审查和技术咨询"{tuple_delimiter}"技术审查"{tuple_delimiter}8){completion_delimiter}
("relationship"{tuple_delimiter}"国家卫生健康委统计信息中心"{tuple_delimiter}"卫生健康信息数据集元数据规范"{tuple_delimiter}"本标准协调和格式审查"{tuple_delimiter}"协调审查"{tuple_delimiter}8){completion_delimiter}
("relationship"{tuple_delimiter}"国家卫生健康委法规司"{tuple_delimiter}"卫生健康信息数据集元数据规范"{tuple_delimiter}"本标准统筹管理"{tuple_delimiter}"统筹管理"{tuple_delimiter}8){completion_delimiter}
("relationship"{tuple_delimiter}"刘建超"{tuple_delimiter}"卫生健康信息数据集元数据规范"{tuple_delimiter}"起草人之一"{tuple_delimiter}"起草"{tuple_delimiter}9){completion_delimiter}
("content_keywords"{tuple_delimiter}"推荐性标准, 标准替代, 技术审查, 协调审查, 业务管理, 统筹管理, 标准起草, 起草人"){completion_delimiter}
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
"""

PROMPTS["entity_continue_extraction"] = """
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
""".strip()

PROMPTS["entity_if_loop_extraction"] = """
---目标---

似乎还有一些实体没有被发现。

---输出---

如果仍然有需要添加的实体，回答“YES”或“NO”（只回答“YES”或“NO”）。
""".strip()

PROMPTS["fail_response"] = (
    "抱歉，我无法回答这个问题。[无相关上下文]"
)

PROMPTS["rag_response"] = """---角色---

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
- 在末尾是否提供参考文献，根据在响应结果是否匹配到有效内容。如果匹配到有效内容，则在末尾提供参考文献。如果没有匹配到有效内容，则不提供参考文献。
- 参考文件的要求与规范
  在响应结果中匹配到有效的内容前提下，在末尾以"参考文献"作为独立章节列出参考文件来源，按照示例中格式展示.格式如下：[序号] 来源内容: file_path内容以及其中的年份。
  - 使用`[序号] [标题/标准编号](URL), 年份`格式
  - 来源内容: file_path内容以及其中的年份
  - 示例：
   参考文献
    [1] [乳酸钾/GB 28101—2019](http://domain-host/#/original?u=1-80861766-f832-4650-9205-600f50c007fc), 2019
    [2] [乳酸锌/GB 28301—2017](http://domain-host/#/original?u=1-12344444-f832-4650-9205-600f50c007fc), 2017

  移除重复参考文件的工作流程
  1. 从每个条目中提取 **"标准编号+年份"** 作为唯一标识键
  2. 提取规范：
   - 从 `[标题/标准编号—年份]` 部分提取
   - **删除所有空格和长破折号**（"—" 或 "–"）
   - **保留字母数字和短横线**
   - 示例：
     * `[乳酸钾/GB 28101—2019]` → `GB28101-2019`
     * `[食品添加剂/WS 217 — 2002]` → `WS217-2002`
     * `[碳酸钙/NY/T 524—2020]` → `NYT524-2020`

  - 去重执行规则
   1. **首次出现原则**：当遇到相同标识键时：
   - 保留**原始列表中第一次出现**的条目
   - **立即删除**后续所有重复条目
   2. **严格匹配**：标识键必须完全一致才视为重复
   - `WS217-2002` ≠ `WS 217-2002`（已通过提取规则解决）
   - `GB28101-2019` ≠ `GB28101-2020`（年份不同不算重复）

  3 输出要求
   a. **仅输出去重后结果**，格式：
   `[新序号] [原始标题/标准编号—年份](原始URL), 原始年份`
   b. **重新编号**：从1开始连续编号
   c. **保留原始内容**：不得修改标题、URL或年份
   d. **禁止添加**：不解释过程、不说明原因
  4. 示例
  错误示例：
    [1] [食品添加剂乳酸锌/WS 217—2002](http://bs.phsciencedata.cn/#/original?u=1-064daeca-34b3-4a65-8f65-74aa6c1fe83a), 2002
    [2] [食品添加剂/WS 217—2002](http://bs.phsciencedata.cn/#/original?u=1-add123df-34b3-4a65-8f65-adde12344), 2002
    [3] [碳酸钙国家标准/WS 216—2001](http://bs.phsciencedata.cn/#/original?u=1-80861766-34b3-4a65-8f65-74321), 2001
    [4] [碳酸钙国家标准/WS 216—2001](http://bs.phsciencedata.cn/#/original?u=1-80861766-34b3-4a65-8f65-74321), 2001
  正确的输出：
    [1] [食品添加剂乳酸锌/WS 217—2002](http://bs.phsciencedata.cn/#/original?u=1-064daeca-34b3-4a65-8f65-74aa6c1fe83a), 2002
    [2] [碳酸钙国家标准/WS 216—2001](http://bs.phsciencedata.cn/#/original?u=1-80861766-34b3-4a65-8f65-74321), 2001
- 如不知道答案或未找到相关内容，请直接说明，在这种情况下，无需提供参考文献。
- 请勿编造信息。不要包含知识库未提供的内容
- 额外的用户提示词:{user_prompt}
响应结果:"""

PROMPTS["keywords_extraction"] = """---角色---

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

"""

PROMPTS["keywords_extraction_examples"] = [
    """示例1:

查询: "国际贸易如何影响全球经济稳定性"
################
Output:
{
  "high_level_keywords": ["国际贸易", "全球经济稳定性", "经济影响"],
  "low_level_keywords":  ["贸易协定", "关税", "货币汇率", "进口", "出口"]
}
#############################""",
    """示例2:

查询: "森林砍伐对生物多样性有哪些环境影响？"
################
输出:
{
  "high_level_keywords": ["环境影响", "森林砍伐", "生物多样性丧失"],
  "low_level_keywords":  ["物种灭绝", "栖息地破坏", "碳排放", "雨林", "生态系统"]
}
#############################""",
    """示例3:

查询: "教育在减少贫困中扮演什么角色？"
################
输出:
{
  "high_level_keywords": ["教育", "减贫", "社会经济发展"],
  "low_level_keywords":  ["入学机会", "识字率", "职业培训", "收入不平等"]
}
#############################""",
]


PROMPTS["naive_rag_response"] = """---角色---

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
- 在末尾是否提供参考文献，根据在响应结果是否匹配到有效内容。如果匹配到有效内容，则在末尾提供参考文献。如果没有匹配到有效内容，则不提供参考文献。
- 参考文件的要求与规范
  在响应结果中匹配到有效的内容前提下，在末尾以"参考文献"作为独立章节列出参考文件来源，按照示例中格式展示.格式如下：[序号] 来源内容: file_path内容以及其中的年份。
  - 使用`[序号] [标题/标准编号](URL), 年份`格式
  - 来源内容: file_path内容以及其中的年份
  - 示例：
   参考文献
    [1] [乳酸钾/GB 28101—2019](http://domain-host/#/original?u=1-80861766-f832-4650-9205-600f50c007fc), 2019
    [2] [乳酸锌/GB 28301—2017](http://domain-host/#/original?u=1-12344444-f832-4650-9205-600f50c007fc), 2017

  移除重复参考文件的工作流程
  1. 从每个条目中提取 **"标准编号+年份"** 作为唯一标识键
  2. 提取规范：
   - 从 `[标题/标准编号—年份]` 部分提取
   - **删除所有空格和长破折号**（"—" 或 "–"）
   - **保留字母数字和短横线**
   - 示例：
     * `[乳酸钾/GB 28101—2019]` → `GB28101-2019`
     * `[食品添加剂/WS 217 — 2002]` → `WS217-2002`
     * `[碳酸钙/NY/T 524—2020]` → `NYT524-2020`

  - 去重执行规则
   1. **首次出现原则**：当遇到相同标识键时：
   - 保留**原始列表中第一次出现**的条目
   - **立即删除**后续所有重复条目
   2. **严格匹配**：标识键必须完全一致才视为重复
   - `WS217-2002` ≠ `WS 217-2002`（已通过提取规则解决）
   - `GB28101-2019` ≠ `GB28101-2020`（年份不同不算重复）

  3 输出要求
   a. **仅输出去重后结果**，格式：
   `[新序号] [原始标题/标准编号—年份](原始URL), 原始年份`
   b. **重新编号**：从1开始连续编号
   c. **保留原始内容**：不得修改标题、URL或年份
   d. **禁止添加**：不解释过程、不说明原因
  4. 示例
  错误示例：
    [1] [食品添加剂乳酸锌/WS 217—2002](http://bs.phsciencedata.cn/#/original?u=1-064daeca-34b3-4a65-8f65-74aa6c1fe83a), 2002
    [2] [食品添加剂/WS 217—2002](http://bs.phsciencedata.cn/#/original?u=1-add123df-34b3-4a65-8f65-adde12344), 2002
    [3] [碳酸钙国家标准/WS 216—2001](http://bs.phsciencedata.cn/#/original?u=1-80861766-34b3-4a65-8f65-74321), 2001
    [4] [碳酸钙国家标准/WS 216—2001](http://bs.phsciencedata.cn/#/original?u=1-80861766-34b3-4a65-8f65-74321), 2001
  正确的输出：
    [1] [食品添加剂乳酸锌/WS 217—2002](http://bs.phsciencedata.cn/#/original?u=1-064daeca-34b3-4a65-8f65-74aa6c1fe83a), 2002
    [2] [碳酸钙国家标准/WS 216—2001](http://bs.phsciencedata.cn/#/original?u=1-80861766-34b3-4a65-8f65-74321), 2001
- 如不知道答案或未找到相关内容，请直接说明，在这种情况下，无需提供参考文献。
- 请勿编造信息。不要包含知识库未提供的内容
- 额外的用户提示词:{user_prompt}

响应结果:"""


# TODO: deprecated
PROMPTS[
    "similarity_check"
] = """请分析以下两个问题的相似度:

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
"""