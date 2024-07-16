from typing import Any, Dict, List

import pandas as pd

import lotus
from lotus.templates import task_instructions


def sem_agg(
    docs: List[str],
    model: lotus.models.LM,
    user_instruction: str,
    partition_ids: List[int],
    **kwargs: Dict[str, Any],
) -> str:
    """
    Aggregates multiple documents into a single answer using a model.

    Args:
        docs (List[str]): The list of documents to aggregate.
        model (lotus.models.LM): The model to use.
        user_instruction (str): The user instruction for aggregation.
        partition_ids (List[int]): The partition ids for the documents. Documents with the same partition id will be aggregated together.
        **kwargs (Dict[str, Any]): Additional keyword arguments.

    Returns:
        str: The aggregated answer.
    """
    leaf_instr_template = (
        "Your job is to provide an answer to the user's instruction given the context below from multiple documents.\n"
        "Remember that your job is to answer the user's instruction by combining all relevant information from all provided documents, into a single coherent answer.\n"
        "Do NOT copy the format of the sources! Instead output your answer in a coherent, well-structured manner that best answers the user instruction.\n"
        "You have limited space to provide your answer, so be concise and to the point.\n\n---\n\n"
        "Follow the following format.\n\nContext: relevant facts from multiple documents\n\n"
        "Instruction: the instruction provided by the user\n\nAnswer: Write your answer\n\n---\n\n"
        "Context: {{docs_str}}\n\n"
        f"Instruction:  {user_instruction}\n\nAnswer:\n"
    )

    node_instr_template = (
        "Your job is to provide an answer to the user's instruction given the context below from multiple sources.\n"
        "Note that each source may be formatted differently and contain information about several different documents.\n"
        "Remember that your job is to answer the user's instruction by combining all relevant information from all provided sources, into a single coherent answer.\n"
        "The sources may provide opposing viewpoints or complementary information.\n"
        "Be sure to include information from ALL relevant sources in your answer.\n"
        "Do NOT copy the format of the sources, instead output your answer in a coherent, well-structured manner that best answers the user instruction.\n"
        "You have limited space to provide your answer, so be concise and to the point.\n"
        "You may need to draw connections between sources to provide a complete answer.\n\n---\n\n"
        "Follow the following format.\n\nContext: relevant facts from multiple sources\n\n"
        "Instruction: the instruction provided by the user\n\nAnswer: Write your answer\n\n---\n\n"
        "Context: {{docs_str}}\n\n"
        f"Instruction:  {user_instruction}\n\nAnswer:\n"
    )

    def leaf_doc_formatter(doc, ctr):
        return f"\n\tDocument {ctr}: {doc}"

    def node_doc_formatter(doc, ctr):
        return f"\n\tSource {ctr}: {doc}"

    def doc_formatter(tree_level, doc, ctr):
        return leaf_doc_formatter(doc, ctr) if tree_level == 0 else node_doc_formatter(doc, ctr)

    tree_level = 0
    summaries = []
    new_partition_ids = []
    while len(docs) != 1 or summaries == []:
        cur_partition_id = partition_ids[0]
        do_fold = len(partition_ids) == len(set(partition_ids))
        context_str = ""
        # prompt = ""
        batch = []
        if tree_level == 0:
            template = leaf_instr_template
        else:
            template = node_instr_template
        template_tokens = model.count_tokens(template)
        context_tokens = 0
        doc_ctr = 1  # num docs in current prompt
        for idx in range(len(docs)):
            partition_id = partition_ids[idx]
            formatted_doc = doc_formatter(tree_level, docs[idx], doc_ctr)
            new_tokens = model.count_tokens(formatted_doc)

            if (
                new_tokens + context_tokens + template_tokens
                > lotus.settings.max_ctx_len - lotus.settings.get_max_tokens()
            ) or (partition_id != cur_partition_id and not do_fold):
                # close the current prompt

                prompt = template.replace("{{docs_str}}", context_str)
                lotus.logger.debug(f"Prompt added to batch: {prompt}")
                batch.append([{"role": "user", "content": prompt}])
                new_partition_ids.append(cur_partition_id)
                cur_partition_id = partition_id
                doc_ctr = 1

                # add new context to next prompt
                formatted_doc = doc_formatter(tree_level, docs[idx], doc_ctr)
                context_str = formatted_doc
                context_tokens = new_tokens
                doc_ctr += 1
            else:
                context_str = context_str + formatted_doc
                context_tokens += new_tokens
                doc_ctr += 1

        if doc_ctr > 1 or len(docs) == 1:
            prompt = template.replace("{{docs_str}}", context_str)
            lotus.logger.debug(f"Prompt added to batch: {prompt}")
            batch.append([{"role": "user", "content": prompt}])
            new_partition_ids.append(cur_partition_id)
        summaries = model(batch, **kwargs)
        partition_ids = new_partition_ids
        new_partition_ids = []

        docs = summaries
        lotus.logger.debug(f"Model outputs from tree level {tree_level}: {summaries}")
        tree_level += 1

    return summaries[0]


@pd.api.extensions.register_dataframe_accessor("sem_agg")
class SemAggDataframe:
    """DataFrame accessor for semantic aggregation."""

    def __init__(self, pandas_obj):
        self._validate(pandas_obj)
        self._obj = pandas_obj

    @staticmethod
    def _validate(obj):
        pass

    def __call__(
        self,
        user_instruction: str,
        suffix: str = "_output",
    ) -> pd.DataFrame:
        """
        Applies semantic aggregation over a dataframe.

        Args:
            user_instruction (str): The user instruction for aggregation.
            suffix (Optional[str]): The suffix for the new column. Defaults to "_output".

        Returns:
            pd.DataFrame: The dataframe with the aggregated answer.
        """

        lotus.logger.debug(user_instruction)
        col_li = lotus.nl_expression.parse_cols(user_instruction)
        lotus.logger.debug(col_li)

        # check that column exists
        for column in col_li:
            if column not in self._obj.columns:
                raise ValueError(f"column {column} not found in DataFrame. Given usr instruction: {user_instruction}")

        # Sort df by partition_id if it exists
        if "_lotus_partition_id" in self._obj.columns:
            self._obj = self._obj.sort_values(by="_lotus_partition_id")
            partition_ids = self._obj["_lotus_partition_id"].tolist()
        else:
            partition_ids = [0] * len(self._obj)

        df_txt = task_instructions.df2text(self._obj, col_li)
        lotus.logger.debug(df_txt)
        formatted_usr_instr = lotus.nl_expression.nle2str(user_instruction, col_li)

        answer = sem_agg(
            df_txt,
            lotus.settings.lm,
            formatted_usr_instr,
            partition_ids,
            **lotus.settings.model_params,
        )

        # package answer in a dataframe
        answer_df = pd.DataFrame([answer], columns=[suffix])
        return answer_df
